"""File-based LangGraph checkpointer.

Stores every checkpoint as JSON under
`~/.ginno/projects/<slug>/sessions/<session_id>.json`.

No database — atomic temp+rename writes, append-only checkpoint list per
session. Supports time-travel resume by checkpoint_id.
"""

from __future__ import annotations

import json
import os
import tempfile
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


def _session_path(project_slug: str, session_id: str) -> Path:
    return paths.project_sessions_dir(project_slug) / f"{session_id}.json"


class FileCheckpointer(BaseCheckpointSaver):
    """One JSON file per session, list of checkpoints inside."""

    serde = JsonPlusSerializer()

    def __init__(self, project_slug: str) -> None:
        super().__init__()
        self.project_slug = project_slug
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
        record = self._read(session_id)
        record["checkpoints"].append(
            {
                "checkpoint_id": cid,
                "parent_id": checkpoint.get("parent_config", {}).get("checkpoint_id"),
                "checkpoint": self.serde.dumps_typed(checkpoint),
                "metadata": self.serde.dumps_typed(metadata),
                "channel_versions": new_versions,
                "ts": time.time(),
            }
        )
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
        checkpoint = self.serde.loads_typed(entry["checkpoint"])
        metadata = self.serde.loads_typed(entry["metadata"])
        return CheckpointTuple(
            config={"configurable": {"thread_id": session_id, "checkpoint_id": entry["checkpoint_id"]}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {"configurable": {"thread_id": session_id, "checkpoint_id": entry["parent_id"]}}
                if entry.get("parent_id")
                else None
            ),
        )

    def put_writes(self, config: dict, writes: list, task_id: str) -> None:
        # P0: no pending writes — rely on put() for full snapshots per step.
        # P1 will implement incremental writes for resumable tasks.
        pass
