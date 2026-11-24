"""Apply a worker's choice to a corresponding parked subscriber question."""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage

from .. import server_shared as shared
from ..checkpointer import FileCheckpointer
from ..session_meta import _session_slug
from . import relay


def _question_rows(payload: dict) -> list[dict]:
    if payload.get("multi"):
        return payload.get("questions") or []
    return [payload]


def selected_options(payload: dict, value: dict) -> list[dict]:
    """Record exact source labels for a single choice or a completed multi card."""
    questions = _question_rows(payload)
    answers = value.get("answers") or [] if value.get("multi") else [value]
    if len(answers) != len(questions):
        return []
    selections = []
    for question, answer in zip(questions, answers):
        if answer.get("skip"):
            return []
        options = question.get("options") or []
        index = answer.get("option_index")
        label = (
            options[index]
            if type(index) is int and 0 <= index < len(options)
            else str(answer.get("answer") or "").strip()
        )
        if not label or options.count(label) != 1:
            return []
        selections.append(
            {"question": question.get("question", ""), "options": options, "answer": label}
        )
    return selections


def _map_selections(selections: list[dict], question: dict) -> dict | None:
    rows = _question_rows(question)
    if not selections or len(rows) != len(selections):
        return None
    answers = []
    remaining = list(selections)
    for row in rows:
        options = row.get("options") or []
        matches = [s for s in selections if sorted(s["options"]) == sorted(options)]
        if len(matches) > 1:
            matches = [s for s in matches if s["question"] == row.get("question")]
        if len(matches) != 1 or matches[0] not in remaining:
            return None
        match = matches[0]
        remaining.remove(match)
        answer = match["answer"]
        answers.append({"answer": answer, "option_index": options.index(answer)})
    return {"multi": True, "answers": answers} if question.get("multi") else answers[0]


def _legacy_selection(envelope: str, answer: str) -> list[dict]:
    """Older tombstones have just the answer text. Read the original option list
    from the trusted relay message and accept only an exact source option."""
    body = envelope.split("\n\n转达方式：\n", 1)[0].split("\n\nHow to relay:\n", 1)[0]
    _, marker, options_text = body.rpartition("\n选项：\n")
    labels = options_text.splitlines()
    if not marker or not labels or any(not line.startswith("- ") for line in labels):
        return []
    options = [line[2:] for line in labels]
    if options.count(answer) != 1:
        return []
    return [{"question": "", "options": options, "answer": answer}]


def _relay_origin(messages: list, call_id: str) -> tuple[str, str, str, str | None] | None:
    """Find the first ask_user after a single server-injected relay request."""
    found_ask = False
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            asks = [call for call in message.tool_calls if call.get("name") == "ask_user"]
            if not asks:
                continue
            if found_ask or len(asks) != 1 or asks[0].get("id") != call_id:
                return None
            found_ask = True
        elif isinstance(message, HumanMessage):
            if message.additional_kwargs.get(shared.STEER_CONTEXT_KEY):
                continue
            ref = message.additional_kwargs.get("ginno_worker_msg") or {}
            if not found_ask or ref.get("kind") != "question":
                return None
            worker, qid = ref.get("from"), ref.get("qid")
            envelope = message.content
            if (
                not worker
                or not qid
                or not isinstance(envelope, str)
                or not envelope.startswith(f'<ginno_worker_question worker="{worker}" qid="{qid}">')
                or envelope.count("<ginno_worker_question ") != 1
            ):
                return None
            steered = message.additional_kwargs.get("ginno_steer") or {}
            return worker, qid, envelope, steered.get("turn_id") or message.id
    return None


def _recent_answer_subscribers(record: dict) -> list[str]:
    if time.time() - int(record.get("answered_at") or 0) > relay._TOMBSTONE_TTL_S:
        return []
    if not record.get("selections", record.get("answer")):
        return []
    if "subscribers" in record:
        return record["subscribers"]
    worker = str(record.get("worker_sid") or "")
    found = relay._find_meta(worker)
    return relay._worker_subscribers(worker, found[0]) if found else []


async def sync_parked_answer(
    session_id: str,
    question: dict | None = None,
    *,
    source_qid: str = "",
    restore: bool = False,
) -> bool:
    """Resume this subscriber's matching card using the recorded worker choice.

    On startup a matching checkpoint can re-arm a parked card. Live calls
    require the original parking guard so another answer wins just once.
    """
    from ..api.sessions import _ensure_session
    from ..api.stream.resume import _rebuild_config, resume_question

    if not restore and shared._PENDING_KIND.get(session_id) != "user_question":
        return False
    candidates = (
        [(source_qid, relay.tombstone_of(source_qid))]
        if source_qid
        else list(relay._RELAY_TOMBSTONES.items())
    )
    records = {
        qid: record
        for qid, record in candidates
        if record and session_id in _recent_answer_subscribers(record)
    }
    if not records or not (slug := _session_slug(session_id)):
        return False
    cp = FileCheckpointer(slug)
    config = {"configurable": {"thread_id": session_id}}
    if question is None:
        pending = await asyncio.to_thread(cp.get_pending_interrupt, config)
        for interrupt in pending or []:
            value = getattr(interrupt, "value", None) or interrupt
            if isinstance(value, dict) and value.get("kind") == "user_question":
                question = value
                break
    if not question or not (call_id := question.get("id")) or not (qid := question.get("qid")):
        return False
    head = await cp.aget_tuple(config)
    if head is None:
        return False
    messages = (head.checkpoint.get("channel_values") or {}).get("messages") or []
    origin = _relay_origin(messages, call_id)
    if origin is None:
        return False
    worker, original_qid, envelope, origin_turn = origin
    record = records.get(original_qid)
    if not record or record.get("worker_sid") != worker:
        return False
    selections = record.get("selections")
    if selections is None:
        selections = _legacy_selection(envelope, str(record.get("answer") or ""))
    answer = _map_selections(selections, question)
    if not answer or not (session := _ensure_session(session_id)):
        return False
    config = _rebuild_config(session)
    if turn_id := shared._RUNNING_TURNS.get(session_id) or origin_turn:
        config["configurable"]["_ui_turn_id"] = turn_id
    ok = await resume_question(
        session_id,
        **answer,
        base_config=config,
        session=session,
        expected_qid=qid,
        arm_from_checkpoint=restore,
        answered_by="worker_sync",
    )
    if ok:
        shared._log.info(
            "relay_choice_synced worker=%s qid=%s sub=%s sub_qid=%s",
            worker,
            original_qid,
            session_id,
            qid,
        )
        try:
            from ..channels.ask_relay import retire_mirrored_question

            await retire_mirrored_question(session_id, qid)
        except Exception:
            shared._log.exception(
                "relay_choice_mirror_cleanup_failed sub=%s qid=%s", session_id, qid
            )
    return ok


async def sync_answered_subscribers(qid: str = "", *, restore: bool = False) -> None:
    records = [relay.tombstone_of(qid)] if qid else list(relay._RELAY_TOMBSTONES.values())
    subscribers = {
        sid for record in records if record for sid in _recent_answer_subscribers(record)
    }
    for sid in subscribers:
        if not restore and shared._PENDING_KIND.get(sid) != "user_question":
            continue
        try:
            async with shared._turn_lock(sid):
                await sync_parked_answer(sid, source_qid=qid, restore=restore)
        except Exception:
            shared._log.exception("relay_choice_sync_failed qid=%s sub=%s", qid, sid)
