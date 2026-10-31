"""File-based LangGraph checkpointer (plan E5: incremental snapshots).

Stores checkpoints as an **append-only JSONL log** under
`~/.ginno/projects/<slug>/sessions/<session_id>.jsonl` — one JSON event per
line, so committing a checkpoint is a single ``O_APPEND`` line write instead
of re-serializing and rewriting the whole session document (a 25 MB session
was rewritten in full on every superstep).

Event kinds (v2 log)::

    {"op":"header","v":2,"session_id":"..."}       # first line, informational
    {"op":"cp", <same fields a v1 entry carried>}  # one checkpoint entry
    {"op":"writes","checkpoint_id":"...","items":[{"task_id","channel","value"}]}
    {"op":"truncate","to":"<checkpoint_id>"}       # rerun_from rewind

Reads fold the log in file order into the same ``{"checkpoints": [...]}``
record shape the rest of the class works with, so v1 semantics are preserved:
``writes`` attach to their checkpoint (or the head when no id was passed) and
``truncate`` cuts the folded tail and re-arms the head for re-execution.
The file itself is never rewritten — not even for rerun_from.

Two entry modes coexist inside an entry:

* ``full``   — the whole checkpoint serialized (legacy behavior; also the
  fallback whenever the message history does not extend the parent's, e.g.
  after compaction rewrites it).
* ``delta``  — only the APPENDED messages since the parent checkpoint, plus
  the small non-message channels and checkpoint statics. Reconstruction
  folds deltas over the parent chain (iteratively, in-memory — the file is
  read once per get_tuple). This is what keeps session files from growing
  quadratically: a 400-step session previously stored the full (ever longer)
  history 400 times.

``put_writes`` persists each superstep's writes under the owning checkpoint
entry (crash forensics + groundwork for mid-step resume); get_tuple keeps
returning ``pending_writes=None`` to preserve today's exact resume semantics.

Legacy v1 files (`<session_id>.json`, one JSON document with a
``checkpoints`` list) stay readable and are migrated to the log on the first
write. Readers always prefer the log when both exist.

``settings.context.checkpoint_mode`` = "delta" (default) | "full".
Reads always handle both modes regardless of the setting.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from ginno_runtime import jsonx as json

from . import paths

_log = logging.getLogger(__name__)

_MESSAGES_CHANNEL = "messages"

_LOG_VERSION = 2

# App-level channels persisted via put_writes for node-internal bookkeeping
# (per-item progress of the parallel gather adapter, stability follow-up
# 2026-08-17). They are crash-durable and readable via get_app_pending, but
# MUST stay invisible to pregel: langgraph treats a task that has pending
# writes as "already finished" and would skip the body node on resume.
_APP_PENDING_CHANNELS = {"parallel_progress"}

# langgraph records a raised interrupt() as a pending write on this channel.
_INTERRUPT_CHANNEL = "__interrupt__"


def _dump_typed(typed: tuple[str, bytes]) -> dict:
    """Serialize serde's (type_tag, bytes) tuple into JSON-safe dict."""
    type_tag, data = typed
    if isinstance(data, (bytes, bytearray)):
        data = base64.b64encode(data).decode("ascii")
    return {"type": type_tag, "data": data}


def _load_typed(stored: dict) -> tuple[str, bytes]:
    """Reconstruct serde's (type_tag, bytes) tuple from JSON-loaded dict."""
    raw = stored["data"]
    if isinstance(raw, str):
        raw = base64.b64decode(raw)
    return (stored["type"], raw)


def checkpoint_path(project_slug: str, session_id: str) -> Path:
    """Canonical append-log path for a session's checkpoints."""
    return paths.project_sessions_dir(project_slug) / f"{session_id}.jsonl"


def legacy_checkpoint_path(project_slug: str, session_id: str) -> Path:
    """Pre-log single-document checkpoint file (read-only; migrated on write)."""
    return paths.project_sessions_dir(project_slug) / f"{session_id}.json"


def existing_checkpoint_path(project_slug: str, session_id: str) -> Path | None:
    """The file readers must use: the log when present, else the legacy doc."""
    log = checkpoint_path(project_slug, session_id)
    if log.exists():
        return log
    legacy = legacy_checkpoint_path(project_slug, session_id)
    return legacy if legacy.exists() else None


def remove_checkpoint_files(project_slug: str, session_id: str) -> bool:
    """Delete every on-disk form of a session's checkpoints. True if any went."""
    removed = False
    for p in (
        checkpoint_path(project_slug, session_id),
        legacy_checkpoint_path(project_slug, session_id),
    ):
        if not p.exists():
            continue
        try:
            p.unlink()
            removed = True
        except OSError:
            _log.warning("checkpoint_unlink_failed path=%s", p)
    return removed


def _header_event(session_id: str) -> dict:
    return {"op": "header", "v": _LOG_VERSION, "session_id": session_id}


def _event_line(event: dict) -> str:
    """One JSONL line (jsonx/orjson escapes newlines inside payload strings)."""
    return json.dumps(event, default=str, ensure_ascii=False) + "\n"


def _write_events_atomic(path: Path, events: list[dict]) -> None:
    """Whole-log write — migration and clone only; commits append (see _append)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(_event_line(e) for e in events)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_legacy(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(doc, dict):
        return []
    cps = doc.get("checkpoints")
    return [c for c in cps if isinstance(c, dict)] if isinstance(cps, list) else []


def _read_log(path: Path) -> list[dict]:
    """Fold the event log into the entry list (see module docstring)."""
    entries: list[dict] = []
    by_id: dict[str, dict] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    last = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except Exception as exc:
            if i == last:
                # Torn tail: the process died between write() and the trailing
                # newline (or mid-line). Nothing follows it, so no committed
                # event is lost by dropping it.
                _log.warning("checkpoint_log_torn_tail path=%s line=%d", path, i + 1)
                break
            raise ValueError(
                f"corrupt checkpoint log {path}: unparsable line {i + 1}"
            ) from exc
        if not isinstance(event, dict):
            continue
        op = event.get("op")
        if op == "cp":
            entry = {k: v for k, v in event.items() if k != "op"}
            cid = entry.get("checkpoint_id")
            if not cid:
                continue
            entries.append(entry)
            by_id[str(cid)] = entry
        elif op == "writes":
            # put_writes: attach to its checkpoint, else to the head (v1 did
            # the same when the config carried no checkpoint_id).
            entry = by_id.get(str(event.get("checkpoint_id") or "")) or (
                entries[-1] if entries else None
            )
            if entry is not None:
                entry.setdefault("pending_writes", []).extend(
                    event.get("items") or []
                )
        elif op == "truncate":
            # rerun_from: the log stays append-only; the folded view drops
            # everything after the cut and re-arms the head for re-execution.
            cid = str(event.get("to") or "")
            idx = next(
                (
                    j
                    for j, c in enumerate(entries)
                    if str(c.get("checkpoint_id")) == cid
                ),
                None,
            )
            if idx is None:
                continue
            del entries[idx + 1 :]
            entries[idx].pop("pending_writes", None)
            by_id = {str(c["checkpoint_id"]): c for c in entries}
        # "header" and unknown ops carry nothing to fold
    return entries


def load_entries(path: Path) -> list[dict]:
    """Checkpoint entries stored in ``path`` (v2 JSONL log or v1 document)."""
    return _read_log(path) if path.suffix == ".jsonl" else _read_legacy(path)


def clone_checkpoint(
    project_slug: str, source_session_id: str, target_session_id: str
) -> Path | None:
    """Copy a session's checkpoint history onto another thread id (retry and
    rerun forks), retagging the embedded session id. Always writes the
    canonical log, so cloning a v1 source migrates it for the fork too.
    Returns the new path, or None when the source has no checkpoint file."""
    src = existing_checkpoint_path(project_slug, source_session_id)
    if src is None:
        return None
    dst = checkpoint_path(project_slug, target_session_id)
    _write_events_atomic(
        dst,
        [_header_event(target_session_id)]
        + [{**e, "op": "cp"} for e in load_entries(src)],
    )
    try:
        legacy_checkpoint_path(project_slug, target_session_id).unlink()
    except OSError:
        pass
    return dst


# Turn ids abandoned by the stall watchdog (server.py). A detached graph run
# can "revive" later (a hung gateway eventually answering) and would then write
# checkpoints that race — and possibly roll back — any retry turn that ran in
# the meantime. The watchdog registers abandoned turn ids here; aput /
# aput_writes refuse to persist them.
ABANDONED_TURNS: set[str] = set()


def _checkpoint_mode() -> str:
    try:
        from .world_state import context_settings

        return str(context_settings().get("checkpoint_mode", "delta"))
    except Exception:
        return "delta"


def _msg_id(m: Any) -> Any:
    return getattr(m, "id", None)


class FileCheckpointer(BaseCheckpointSaver):
    """One append-only JSONL log per session (see module docstring)."""

    serde = JsonPlusSerializer()

    def __init__(self, project_slug: str, surface_pending_writes: bool = False) -> None:
        super().__init__()
        self.project_slug = project_slug
        # When True, get_tuple returns stored put_writes (minus app-level
        # channels) so langgraph gets its standard mid-step resume semantics:
        # tasks that already completed inside an interrupted superstep are not
        # re-executed. The workflow engine opts in; chat keeps the historical
        # pending_writes=None behavior for byte-identical resume semantics.
        self.surface_pending_writes = surface_pending_writes
        # Serializes read-modify-write cycles on the session file. aput and
        # aput_writes run on thread-pool executors and LangGraph may drive them
        # concurrently (e.g. the interrupted superstep); without this lock the
        # two writers race and one checkpoint entry is silently lost. Reads are
        # lock-free: _write is atomic (temp+rename), so readers always see a
        # complete file.
        self._write_lock = threading.RLock()
        paths.project_sessions_dir(project_slug).mkdir(parents=True, exist_ok=True)

    def _read(self, session_id: str) -> dict[str, Any]:
        path = existing_checkpoint_path(self.project_slug, session_id)
        if path is None:
            return {"session_id": session_id, "checkpoints": []}
        return {"session_id": session_id, "checkpoints": load_entries(path)}

    def _migrate_legacy(self, session_id: str) -> None:
        """Fold a v1 single-document file into the append log (one-time)."""
        log = checkpoint_path(self.project_slug, session_id)
        legacy = legacy_checkpoint_path(self.project_slug, session_id)
        if log.exists() or not legacy.exists():
            return
        _write_events_atomic(
            log,
            [_header_event(session_id)]
            + [{**e, "op": "cp"} for e in _read_legacy(legacy)],
        )
        try:
            legacy.unlink()
        except OSError:
            _log.warning("checkpoint_legacy_unlink_failed path=%s", legacy)

    def _repair_tail(self, log: Path) -> None:
        """Drop a torn last line left by a crash mid-append.

        Readers already ignore an unparsable LAST line, but a later append
        would turn that line into interior corruption — which they refuse to
        skip. Trimming the incomplete tail before every commit keeps the log
        parseable after a crash (only the uncommitted event is lost)."""
        try:
            with open(log, "rb+") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size == 0:
                    return
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) == b"\n":
                    return
                pos = size - 1
                while pos > 0:
                    step = min(4096, pos)
                    pos -= step
                    fh.seek(pos)
                    if (nl := fh.read(step).rfind(b"\n")) != -1:
                        os.truncate(log, pos + nl + 1)
                        return
                os.truncate(log, 0)
        except OSError:
            _log.warning("checkpoint_tail_repair_failed path=%s", log)

    def _append(self, session_id: str, events: list[dict]) -> None:
        """Commit events as one O_APPEND write — never a full-file rewrite."""
        with self._write_lock:
            self._migrate_legacy(session_id)
            log = checkpoint_path(self.project_slug, session_id)
            payload = "".join(_event_line(e) for e in events)
            if log.exists():
                self._repair_tail(log)
            else:
                payload = _event_line(_header_event(session_id)) + payload
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(payload)

    # ---- reconstruction helpers ------------------------------------------- #
    def _entries_by_id(self, record: dict) -> dict[str, dict]:
        return {c["checkpoint_id"]: c for c in record.get("checkpoints", [])}

    def _reconstruct_checkpoint(self, record: dict, entry: dict) -> Checkpoint:
        """Materialize a checkpoint entry (full or delta chain) into a Checkpoint."""
        if entry.get("mode", "full") == "full" or "checkpoint" in entry:
            return self.serde.loads_typed(_load_typed(entry["checkpoint"]))

        # delta: walk the parent chain down to a full anchor, then fold forward.
        chain: list[dict] = [entry]
        seen: set[int] = {id(entry)}
        cur = entry
        by_id = self._entries_by_id(record)
        while cur.get("mode") == "delta" and "checkpoint" not in cur:
            base = by_id.get(cur.get("base"))
            if base is None:
                raise ValueError(
                    f"checkpoint chain broken: missing base {cur.get('base')} "
                    f"for {cur.get('checkpoint_id')}"
                )
            if id(base) in seen:
                # A re-put checkpoint dict reusing its parent's id links an
                # entry to itself; without this guard the walk would spin
                # forever and every read of the session would hang.
                raise ValueError(
                    f"checkpoint chain cycle at {cur.get('checkpoint_id')} "
                    f"(base {cur.get('base')})"
                )
            seen.add(id(base))
            chain.append(base)
            cur = base
        checkpoint = self.serde.loads_typed(_load_typed(chain[-1]["checkpoint"]))
        for delta_entry in reversed(chain[:-1]):
            checkpoint = self._apply_delta(checkpoint, delta_entry)
        return checkpoint

    def _apply_delta(self, base: Checkpoint, delta_entry: dict) -> Checkpoint:
        static = self.serde.loads_typed(_load_typed(delta_entry["static"]))
        base_values = base.get("channel_values") or {}
        # channel_values is rebuilt ONLY from this delta's channels — it must
        # NOT be seeded from ``base``. A delta records exactly the channels
        # present in its own checkpoint's ``channel_values`` (messages as an
        # append diff, everything else full). Langgraph drops consumed trigger
        # channels (``__start__``, ``branch:to:*``) from ``channel_values`` in
        # later checkpoints; inheriting them from the anchor would resurrect
        # stale triggers and corrupt ``StateSnapshot.next`` (the stale
        # ``branch:to:*`` re-fires the agent node on every read).
        values: dict[str, Any] = {}
        for channel, payload in (delta_entry.get("channels") or {}).items():
            if payload.get("mode") == "append":
                extra = self.serde.loads_typed(_load_typed(payload["items"]))
                values[channel] = list(base_values.get(channel) or []) + list(extra)
            else:
                values[channel] = self.serde.loads_typed(_load_typed(payload["value"]))
        static["channel_values"] = values
        return static

    def _try_messages_delta(
        self, record: dict, parent_id: str, checkpoint: Checkpoint
    ) -> dict | None:
        """Return a delta entry if the new checkpoint extends the parent's
        message history by pure append; None otherwise (caller stores full)."""
        by_id = self._entries_by_id(record)
        parent_entry = by_id.get(parent_id)
        if parent_entry is None:
            return None
        try:
            parent_cp = self._reconstruct_checkpoint(record, parent_entry)
        except Exception:
            return None
        parent_values = parent_cp.get("channel_values") or {}
        new_values = checkpoint.get("channel_values") or {}
        if _MESSAGES_CHANNEL not in new_values:
            return None
        parent_msgs = list(parent_values.get(_MESSAGES_CHANNEL) or [])
        new_msgs = list(new_values.get(_MESSAGES_CHANNEL) or [])
        if len(new_msgs) < len(parent_msgs):
            return None
        # Rewrite detection is by message ID only (content is not diffed — that
        # would cost an O(prefix) serialize per put, the very quadratic blow-up
        # delta mode exists to avoid). INVARIANT: any writer that edits an
        # existing message's content MUST give it a new id so the mismatch here
        # forces a full snapshot. Compaction and microcompact both honor this
        # (they re-add every message with a fresh id); a same-id in-place edit
        # would be silently lost when the delta chain is reconstructed.
        for a, b in zip(parent_msgs, new_msgs):
            if _msg_id(a) != _msg_id(b):
                return None  # history rewritten (compaction/rollback) → full

        channels: dict[str, dict] = {
            _MESSAGES_CHANNEL: {
                "mode": "append",
                "items": _dump_typed(self.serde.dumps_typed(new_msgs[len(parent_msgs):])),
            }
        }
        for ch, val in new_values.items():
            if ch == _MESSAGES_CHANNEL:
                continue
            channels[ch] = {"mode": "full", "value": _dump_typed(self.serde.dumps_typed(val))}
        static = {k: v for k, v in checkpoint.items() if k != "channel_values"}
        return {
            "mode": "delta",
            "base": parent_id,
            "static": _dump_typed(self.serde.dumps_typed(static)),
            "channels": channels,
        }

    # ---- LangGraph BaseCheckpointSaver API (sync) ----
    def put(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:  # type: ignore[override]
        session_id = config["configurable"]["thread_id"]
        cid = str(checkpoint.get("id") or uuid.uuid4().hex)
        # langgraph（锁版 1.2.x）把 PARENT checkpoint id 放在 ``config`` 里，
        # checkpoint dict 不再携带 ``parent_config``（实测：put N+1 的
        # config["configurable"]["checkpoint_id"] == put N 写入的 checkpoint id）。
        # 旧版（<1.x）的 parent_config 兼容读取已随锁版移除——从 parent_config
        # 读父 id 曾静默关掉 delta 模式，每个 superstep 存全量、会话文件平方级膨胀。
        parent_id = (config.get("configurable") or {}).get("checkpoint_id")
        with self._write_lock:
            record = self._read(session_id)

            if not parent_id and record.get("checkpoints"):
                # Last resort (direct put() calls carrying no parent info):
                # in a linear flow the parent is the last checkpoint stored.
                # Safe even when the guess is wrong (branch/rewind):
                # ``_try_messages_delta`` re-validates the pure-append
                # invariant and degrades to a full snapshot instead of
                # corrupting the chain.
                parent_id = record["checkpoints"][-1]["checkpoint_id"]

            entry: dict[str, Any] = {
                "checkpoint_id": cid,
                "parent_id": parent_id,
                "metadata": _dump_typed(self.serde.dumps_typed(metadata)),
                "channel_versions": new_versions,
                "ts": time.time(),
            }
            delta = None
            # ``parent_id == cid`` means the same checkpoint dict is being put
            # twice (langgraph never does this; a manual re-put might). A delta
            # onto itself would make the chain cyclic and permanently unreadable.
            if _checkpoint_mode() == "delta" and parent_id and parent_id != cid:
                delta = self._try_messages_delta(record, parent_id, checkpoint)
            if delta is not None:
                entry.update(delta)
            else:
                entry["mode"] = "full"
                entry["checkpoint"] = _dump_typed(self.serde.dumps_typed(checkpoint))
            self._append(session_id, [{**entry, "op": "cp"}])
        return {"configurable": {"thread_id": session_id, "checkpoint_id": cid}}

    def _tuple_from(self, record: dict, session_id: str, entry: dict) -> Any:
        """Build one CheckpointTuple from a stored entry (shared by get_tuple
        and list)."""
        checkpoint = self._reconstruct_checkpoint(record, entry)
        metadata = self.serde.loads_typed(_load_typed(entry["metadata"]))
        pending = None
        if self.surface_pending_writes:
            # Standard langgraph mid-step resume: completed tasks inside an
            # interrupted superstep are skipped on re-entry. App-level
            # bookkeeping channels are filtered out (see _APP_PENDING_CHANNELS).
            pending = [
                (
                    w.get("task_id") or "",
                    w["channel"],
                    self.serde.loads_typed(_load_typed(w["value"])),
                )
                for w in (entry.get("pending_writes") or [])
                if w.get("channel") not in _APP_PENDING_CHANNELS
            ] or None
        return CheckpointTuple(
            config={"configurable": {"thread_id": session_id, "checkpoint_id": entry["checkpoint_id"]}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {"configurable": {"thread_id": session_id, "checkpoint_id": entry["parent_id"]}}
                if entry.get("parent_id")
                else None
            ),
            pending_writes=pending,
        )

    def get_tuple(self, config: dict) -> Any:
        session_id = config["configurable"]["thread_id"]
        record = self._read(session_id)
        cps = record.get("checkpoints", [])
        if not cps:
            return None
        target = config["configurable"].get("checkpoint_id")
        if target:
            entry = next((c for c in cps if c["checkpoint_id"] == target), None)
        else:
            entry = cps[-1]
        if not entry:
            return None
        return self._tuple_from(record, session_id, entry)

    def get_pending_interrupt(self, config: dict) -> list | None:
        """The parked turn's interrupt(s), or None if the graph is not suspended.

        Read-only companion for the reconnect re-emit in ``api/stream.py``. That
        path used ``graph.aget_state().tasks[].interrupts``, which is unreliable
        here for two independent reasons:

        * ``get_tuple`` hides pending writes by default (see
          ``surface_pending_writes``), so langgraph rebuilds no interrupts; and
        * even with them surfaced it still misses the common case — an interrupt
          raised inside the ``tools`` node gets one extra WRITE-LESS checkpoint
          committed on top of it, so the newest entry carries no
          ``__interrupt__``. (Measured: a permission-node interrupt lands on the
          newest entry, a tools-node one — every ``ask_user``, and
          ``workflow_propose_edit`` — lands one entry back.)

        Liveness rule: walk back to the newest entry that has ANY pending
        writes. ``__interrupt__`` there means the graph is still suspended; any
        ordinary channel write means the turn moved on (resumed, or ran to the
        next superstep) and a stale interrupt must never be replayed.
        """
        session_id = config["configurable"]["thread_id"]
        record = self._read(session_id)
        for entry in reversed(record.get("checkpoints") or []):
            writes = entry.get("pending_writes") or []
            if not writes:
                continue
            for w in writes:
                if w.get("channel") != _INTERRUPT_CHANNEL:
                    continue
                try:
                    return self.serde.loads_typed(_load_typed(w["value"]))
                except Exception:
                    return None
            # Newest writes are ordinary channel updates → the turn is running.
            return None
        return None

    def get_app_pending(self, config: dict, channel: str) -> Any:
        """Latest app-level pending write for ``channel`` (last write wins).

        Companion to :data:`_APP_PENDING_CHANNELS`: nodes persist incremental
        bookkeeping (parallel gather per-item progress) via ``put_writes`` and
        read it back here on re-execution, skipping already-finished work after
        a pause/resume or a crash-retry (the retry endpoint copies the whole
        checkpoint file, pending writes included)."""
        if channel not in _APP_PENDING_CHANNELS:
            return None
        session_id = config["configurable"]["thread_id"]
        record = self._read(session_id)
        cps = record.get("checkpoints", [])
        target = config["configurable"].get("checkpoint_id")
        entries = reversed(cps) if not target else (
            c for c in reversed(cps) if c["checkpoint_id"] == target
        )
        for entry in entries:
            for w in reversed(entry.get("pending_writes") or []):
                if w.get("channel") == channel:
                    try:
                        return self.serde.loads_typed(_load_typed(w["value"]))
                    except Exception:
                        return None
        return None

    def put_writes(self, config: dict, writes: list, task_id: str) -> None:
        """Persist a superstep's writes as log events owned by their checkpoint.

        Stored for crash forensics and future mid-step resume; get_tuple does
        not expose them yet (behavior parity). Serialization failures are
        swallowed — losing the forensics copy must never break a turn.
        """
        try:
            session_id = config["configurable"]["thread_id"]
            cid = (config.get("configurable") or {}).get("checkpoint_id")
            items: list[dict] = []
            for channel, value in writes or []:
                try:
                    items.append(
                        {
                            "task_id": task_id,
                            "channel": channel,
                            "value": _dump_typed(self.serde.dumps_typed(value)),
                        }
                    )
                except Exception:
                    continue
            if items:
                self._append(
                    session_id,
                    [{"op": "writes", "checkpoint_id": cid, "items": items}],
                )
        except Exception:
            pass

    def truncate_to(self, session_id: str, checkpoint_id: str) -> bool:
        """Make ``checkpoint_id`` the HEAD of the session's history (rerun-from).

        Appends a ``truncate`` event instead of rewriting the file: the folded
        view drops everything after the cut (later supersteps, newer
        interrupts) and clears the head's pending writes so langgraph
        re-executes the node instead of treating it as already finished.
        Returns False when the id is unknown or already the head (parity with
        the previous in-place rewrite, which left the head untouched).
        """
        with self._write_lock:
            cps = self._read(session_id).get("checkpoints") or []
            idx = next(
                (
                    i
                    for i, c in enumerate(cps)
                    if str(c.get("checkpoint_id")) == str(checkpoint_id)
                ),
                None,
            )
            if idx is None or idx == len(cps) - 1:
                return False
            self._append(session_id, [{"op": "truncate", "to": checkpoint_id}])
            return True

    # ---- LangGraph BaseCheckpointSaver API (async) ----
    async def aget_tuple(self, config: dict) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> Any:
        if (config.get("configurable") or {}).get("turn_id") in ABANDONED_TURNS:
            return config  # abandoned run: never persist, can't roll back retries
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(self, config: dict, writes: list, task_id: str) -> None:
        if (config.get("configurable") or {}).get("turn_id") in ABANDONED_TURNS:
            return
        return await asyncio.to_thread(self.put_writes, config, writes, task_id)

    def list(
        self, config: dict, *, filter: dict | None = None, before: Any = None, limit: int | None = None
    ) -> Any:
        """Checkpoint history, newest first (design B P2: run rerun-from-node).

        The entries are already stored append-only with parent links, so a
        listing is just a reverse walk with tuple materialization. ``before``
        is exclusive (langgraph convention: stop at that checkpoint id);
        ``limit`` counts yielded tuples. A broken delta chain skips that entry
        rather than killing the whole listing — history consumers (rerun_from)
        prefer a partial listing over none. ``filter`` (source/step metadata)
        is not supported."""
        session_id = config["configurable"]["thread_id"]
        record = self._read(session_id)
        before_id = None
        if isinstance(before, dict):
            before_id = (before.get("configurable") or before).get("checkpoint_id")
        out: list[Any] = []
        for entry in reversed(record.get("checkpoints", [])):
            if before_id is not None and entry.get("checkpoint_id") == before_id:
                break
            try:
                out.append(self._tuple_from(record, session_id, entry))
            except Exception:
                continue
            if limit is not None and len(out) >= limit:
                break
        return iter(out)

    async def alist(
        self, config: dict, *, filter: dict | None = None, before: Any = None, limit: int | None = None
    ) -> Any:
        for tup in await asyncio.to_thread(
            self.list, config, filter=filter, before=before, limit=limit
        ):
            yield tup
