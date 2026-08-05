"""File-based LangGraph checkpointer (plan E5: incremental snapshots).

Stores checkpoints as JSON under
`~/.ginno/projects/<slug>/sessions/<session_id>.json`.

Two entry modes coexist in one file:

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

``settings.context.checkpoint_mode`` = "delta" (default) | "full".
Reads always handle both modes regardless of the setting.
"""

from __future__ import annotations

import asyncio
import base64
import json
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

from . import paths

_MESSAGES_CHANNEL = "messages"


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


def _session_path(project_slug: str, session_id: str) -> Path:
    return paths.project_sessions_dir(project_slug) / f"{session_id}.json"


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
    """One JSON file per session, append-only list of (full|delta) checkpoints."""

    serde = JsonPlusSerializer()

    def __init__(self, project_slug: str) -> None:
        super().__init__()
        self.project_slug = project_slug
        # Serializes read-modify-write cycles on the session file. aput and
        # aput_writes run on thread-pool executors and LangGraph may drive them
        # concurrently (e.g. the interrupted superstep); without this lock the
        # two writers race and one checkpoint entry is silently lost. Reads are
        # lock-free: _write is atomic (temp+rename), so readers always see a
        # complete file.
        self._write_lock = threading.RLock()
        paths.project_sessions_dir(project_slug).mkdir(parents=True, exist_ok=True)

    def _read(self, session_id: str) -> dict[str, Any]:
        f = _session_path(self.project_slug, session_id)
        if not f.exists():
            return {"session_id": session_id, "checkpoints": []}
        return json.loads(f.read_text() or "{}")

    def _write(self, data: dict[str, Any]) -> None:
        f = _session_path(self.project_slug, data["session_id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        # atomic: write temp + rename
        fd, tmp = tempfile.mkstemp(dir=f.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, default=str, ensure_ascii=False)
            os.replace(tmp, f)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---- reconstruction helpers ------------------------------------------- #
    def _entries_by_id(self, record: dict) -> dict[str, dict]:
        return {c["checkpoint_id"]: c for c in record.get("checkpoints", [])}

    def _reconstruct_checkpoint(self, record: dict, entry: dict) -> Checkpoint:
        """Materialize a checkpoint entry (full or delta chain) into a Checkpoint."""
        if entry.get("mode", "full") == "full" or "checkpoint" in entry:
            return self.serde.loads_typed(_load_typed(entry["checkpoint"]))

        # delta: walk the parent chain down to a full anchor, then fold forward.
        chain: list[dict] = [entry]
        cur = entry
        by_id = self._entries_by_id(record)
        while cur.get("mode") == "delta" and "checkpoint" not in cur:
            base = by_id.get(cur.get("base"))
            if base is None:
                raise ValueError(
                    f"checkpoint chain broken: missing base {cur.get('base')} "
                    f"for {cur.get('checkpoint_id')}"
                )
            chain.append(base)
            cur = base
        checkpoint = self.serde.loads_typed(_load_typed(chain[-1]["checkpoint"]))
        for delta_entry in reversed(chain[:-1]):
            checkpoint = self._apply_delta(checkpoint, delta_entry)
        return checkpoint

    def _apply_delta(self, base: Checkpoint, delta_entry: dict) -> Checkpoint:
        static = self.serde.loads_typed(_load_typed(delta_entry["static"]))
        values = dict(base.get("channel_values") or {})
        for channel, payload in (delta_entry.get("channels") or {}).items():
            if payload.get("mode") == "append":
                extra = self.serde.loads_typed(_load_typed(payload["items"]))
                values[channel] = list(values.get(channel) or []) + list(extra)
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
        parent_cfg = checkpoint.get("parent_config") or {}
        # parent_config is a RunnableConfig ({"configurable": {...}}); tolerate
        # a flat shape too.
        parent_id = (parent_cfg.get("configurable") or {}).get(
            "checkpoint_id"
        ) or parent_cfg.get("checkpoint_id")
        with self._write_lock:
            record = self._read(session_id)

            entry: dict[str, Any] = {
                "checkpoint_id": cid,
                "parent_id": parent_id,
                "metadata": _dump_typed(self.serde.dumps_typed(metadata)),
                "channel_versions": new_versions,
                "ts": time.time(),
            }
            delta = None
            if _checkpoint_mode() == "delta" and parent_id:
                delta = self._try_messages_delta(record, parent_id, checkpoint)
            if delta is not None:
                entry.update(delta)
            else:
                entry["mode"] = "full"
                entry["checkpoint"] = _dump_typed(self.serde.dumps_typed(checkpoint))
            record["checkpoints"].append(entry)
            self._write(record)
        return {"configurable": {"thread_id": session_id, "checkpoint_id": cid}}

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
        checkpoint = self._reconstruct_checkpoint(record, entry)
        metadata = self.serde.loads_typed(_load_typed(entry["metadata"]))
        return CheckpointTuple(
            config={"configurable": {"thread_id": session_id, "checkpoint_id": entry["checkpoint_id"]}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {"configurable": {"thread_id": session_id, "checkpoint_id": entry["parent_id"]}}
                if entry.get("parent_id")
                else None
            ),
            # NOTE: stored per-step writes (put_writes) are intentionally NOT
            # surfaced here yet — preserves the pre-E5 resume semantics exactly.
        )

    def put_writes(self, config: dict, writes: list, task_id: str) -> None:
        """Persist a superstep's writes under the owning checkpoint entry.

        Stored for crash forensics and future mid-step resume; get_tuple does
        not expose them yet (behavior parity). Serialization failures are
        swallowed — losing the forensics copy must never break a turn.
        """
        try:
            session_id = config["configurable"]["thread_id"]
            cid = (config.get("configurable") or {}).get("checkpoint_id")
            with self._write_lock:
                record = self._read(session_id)
                cps = record.get("checkpoints", [])
                entry = next(
                    (c for c in reversed(cps) if c["checkpoint_id"] == cid),
                    cps[-1] if cps else None,  # no checkpoint_id given → latest
                )
                if entry is None:
                    return
                stored = entry.setdefault("pending_writes", [])
                for channel, value in writes or []:
                    try:
                        stored.append(
                            {
                                "task_id": task_id,
                                "channel": channel,
                                "value": _dump_typed(self.serde.dumps_typed(value)),
                            }
                        )
                    except Exception:
                        continue
                self._write(record)
        except Exception:
            pass

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
        # Minimal — no listing. Time-travel uses get_tuple(checkpoint_id).
        return iter([])

    async def alist(
        self, config: dict, *, filter: dict | None = None, before: Any = None, limit: int | None = None
    ) -> Any:
        return iter([])
