"""E2E: universal code-image surfacing (image-emit-universal design).

Before the fix, ``image.emit`` only fired from the bash-marker branch of
``_tool_file_effects`` — pictures registered by any OTHER producer (browser
screenshots landing on disk, MCP image writes, write-then-register tools)
never broadcast, and since the durable ``ginno_images`` anchor was also lifted
from bash markers only, they vanished from history replay too.

These tests drive the real server + real tool loop with a fake model that
simulates a non-bash producer (writes a picture and registers it — exactly
what the browser/MCP paths do, minus the marker) and assert:

(a) live: the WS stream carries ``image.emit`` with a downloadable file_id;
(b) replay: GET /history rebuilds the image block (durable anchor);
(c) regression: the bash path still emits exactly once (no double-fire now
    that a generic fallback also diffs the registry);
(d) baseline: a picture registered BEFORE the turn (an upload) is not
    re-broadcast as if the turn produced it.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ginno_runtime import paths
from ginno_runtime.files import get_registry, reset_registries

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _fresh_files(isolated_home):
    reset_registries()
    yield
    reset_registries()


@pytest.fixture(autouse=True)
def _bypass_on(client, isolated_home):
    """These tests exercise the image wiring, not the permission flow."""
    from ginno_runtime import jsonx as json

    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(json.dumps(s))


def _png_bytes() -> bytes:
    """A minimal valid 1x1 PNG (registry classifies by extension, the
    download endpoint just serves bytes — but keep it a real picture)."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class ProducerModel:
    """Simulates a NON-bash image producer.

    First model call writes a picture into the session files dir and
    REGISTERS it (kind=image) — the same effect a browser-screenshot tool or
    an MCP image write has — then calls an innocuous tool so the post-tool
    effect hook runs. No bash, no ``<!--ginno-images:-->`` marker anywhere.
    """

    def __init__(self, sid_getter, *, name: str = "shot.png"):
        # sid resolved lazily: the model instance must exist before
        # create_session() mints the id.
        self._sid_getter = sid_getter
        self.name = name
        self.calls = 0

    @property
    def sid(self) -> str:
        return self._sid_getter()

    def bind_tools(self, tools):
        return self

    def _produce(self) -> Path:
        p = paths.session_files_dir("default", self.sid) / self.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_png_bytes())
        get_registry("default").register(
            self.name, str(p), kind="image", session_id=self.sid
        )
        return p

    async def ainvoke(self, messages, **kw):
        from langchain_core.messages import AIMessage

        self.calls += 1
        if self.calls == 1:
            self._produce()
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": self.name}, "id": "c1"}
                ],
            )
        return AIMessage(content="截图已生成。")


def _run_turn(client, ws_conv, sid: str, model) -> list[dict]:
    # create_session patched build_model to THIS model already; drive one turn.
    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "截个图"})
        events = conv.recv_until("message.end", "error")
    assert "error" not in [e["event"] for e in events], events
    return events


def test_nonbash_registered_image_emits_inline(
    client, create_session, ws_conv, isolated_home
):
    """(a) A picture registered by a non-bash producer broadcasts image.emit
    with a downloadable file_id — the generic registry-diff fallback."""
    sid_holder: dict[str, str] = {}

    model = ProducerModel(lambda: sid_holder["sid"])
    sid_holder["sid"] = create_session(model)
    events = _run_turn(client, ws_conv, sid_holder["sid"], model)

    emits = [e for e in events if e["event"] == "image.emit"]
    assert emits, (
        f"no image.emit for a registered (non-bash) picture in "
        f"{[e['event'] for e in events]}"
    )
    ev = emits[0]
    assert ev["name"] == "shot.png"
    assert isinstance(ev["mtime"], int)

    # registered artifact (the fallback backfills one when missing)
    arts = client.get("/api/artifacts?project_slug=default").json()
    assert any(
        a["kind"] == "image" and a["session_id"] == sid_holder["sid"]
        for a in arts
    ), arts

    # the bytes are served for inline display
    r = client.get(f"/api/files/{ev['file_id']}/download")
    assert r.status_code == 200 and r.content == _png_bytes()


def test_nonbash_image_survives_replay(
    client, create_session, ws_conv, isolated_home
):
    """(b) Refresh/reopen: GET /history rebuilds the image block from the
    durable ginno_images anchor (the agent-node lift diffed the baseline)."""
    sid_holder: dict[str, str] = {}

    model = ProducerModel(lambda: sid_holder["sid"])
    sid_holder["sid"] = create_session(model)
    events = _run_turn(client, ws_conv, sid_holder["sid"], model)
    fid = next(e["file_id"] for e in events if e["event"] == "image.emit")

    # "refresh": a fresh history read (server-side message structure — the
    # frontend maps {kind:image, fileId} blocks straight to imageUrl()).
    msgs = client.get(f"/api/sessions/{sid_holder['sid']}/history").json()["messages"]
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    imgs = [b for b in assistant["blocks"] if b["kind"] == "image"]
    assert imgs, f"no image block on replay: {assistant['blocks']}"
    assert imgs[0]["fileId"] == fid
    assert imgs[0]["name"] == "shot.png"
    assert isinstance(imgs[0]["mtime"], int)

    # the anchor itself is persisted on the final AIMessage (survives
    # microcompact, which clears ToolMessage bodies)
    from ginno_runtime.checkpointer import checkpoint_path

    log = checkpoint_path("default", sid_holder["sid"])
    assert log.is_file(), f"session log missing: {log}"
    # the anchor itself is persisted on the final AIMessage (survives
    # microcompact, which clears ToolMessage bodies). Messages ride the
    # append-only log as base64 typed-bytes, so decode the deltas and inspect
    # the deserialized messages rather than grepping raw lines.
    import json as _json

    from ginno_runtime.checkpointer import FileCheckpointer, _load_typed

    anchor_paths: list[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            d = _json.loads(line)
        except ValueError:
            continue
        if d.get("op") != "cp":
            continue
        ch = (d.get("channels") or {}).get("messages") or {}
        raw_items = ch.get("items") if ch.get("mode") == "append" else None
        # ``items`` is the typed-blob dict itself ({"type","data"}); accept a
        # list of them too in case the log shape ever widens.
        items = raw_items if isinstance(raw_items, list) else (
            [raw_items] if isinstance(raw_items, dict) and "data" in raw_items else []
        )
        for it in items:
            try:
                msgs = FileCheckpointer("default").serde.loads_typed(_load_typed(it))
            except Exception:
                continue
            for m in msgs or []:
                gi = (getattr(m, "additional_kwargs", None) or {}).get("ginno_images")
                if isinstance(gi, list):
                    anchor_paths.extend(str(x) for x in gi)
    assert any(p.endswith("shot.png") for p in anchor_paths), (
        f"ginno_images anchor not persisted: {anchor_paths}"
    )


def test_bash_image_emits_exactly_once(
    client, create_session, ws_conv, isolated_home
):
    """(c) Bash-marker path regression: with the generic fallback now also
    diffing the registry, the bash-emitted picture must NOT double-fire."""
    from ginno_runtime.testing.fake_model import script, script_tool_call

    scripted = [
        script(
            tool_calls=[
                script_tool_call("bash", {"command": "printf x > chart.png"})
            ]
        ),
        script(text="图已生成。"),
    ]
    sid = create_session(scripted)
    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "画个图"})
        events = conv.recv_until("message.end", "error")

    emits = [e for e in events if e["event"] == "image.emit"]
    assert len(emits) == 1, f"bash picture emitted {len(emits)}x: {emits}"
    # and replay still has it (marker lift path unchanged)
    msgs = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assistant = [m for m in msgs if m["role"] == "assistant"][0]
    imgs = [b for b in assistant["blocks"] if b["kind"] == "image"]
    assert len(imgs) == 1 and imgs[0]["fileId"] == emits[0]["file_id"]


def test_preexisting_image_not_reemitted(
    client, create_session, ws_conv, isolated_home
):
    """(d) Baseline: a picture registered BEFORE the turn (an upload) is in
    the turn-start snapshot — an ordinary tool turn must not broadcast it."""
    from ginno_runtime.testing.fake_model import script, script_tool_call

    sid = create_session(
        [
            script(tool_calls=[script_tool_call("read_file", {"path": "note.txt"})]),
            script(text="看完了。"),
        ]
    )

    # pre-turn registration (like an upload path would do)
    p = paths.session_files_dir("default", sid) / "uploaded.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_png_bytes())
    get_registry("default").register(
        "uploaded.png", str(p), kind="image", session_id=sid
    )

    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "读一下"})
        events = conv.recv_until("message.end", "error")

    emits = [e for e in events if e["event"] == "image.emit"]
    assert not emits, f"pre-turn picture re-broadcast: {emits}"
