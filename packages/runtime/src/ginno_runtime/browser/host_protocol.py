"""Native messaging host protocol (browser-companion design §2 备用传输).

Importable copy — used by BOTH:
- the dev-mode standalone shim (~/.ginno/browser-extension/ginno_browser_host.py,
  shebang = sidecar venv python, which has ginno_runtime installed)
- the frozen binary's ``ginno-runtime --native-host`` mode (in-process)

Protocol: length-prefixed JSON on stdin/stdout. ``getPort``/``ping`` are
answered inline; ``relayConnect`` opens a websocket bridge to the sidecar
relay (ws://127.0.0.1:<port>/extension/v2) and pipes ``relayData`` /
``relayDataChunk`` frames both ways (chunking stays under the native
messaging size cap, reassembled on the other side).
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
from pathlib import Path

CHUNK = 256 * 1024


def port_file() -> Path:
    home = os.environ.get("GINNO_HOME")
    return Path(home or "~/.ginno").expanduser() / "browser-relay-port"


def read_port() -> int | None:
    try:
        return int(port_file().read_text().strip())
    except (OSError, ValueError):
        return None


def read_msg() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("@I", raw)
    try:
        return json.loads(sys.stdin.buffer.read(n))
    except (ValueError, TypeError):
        return {}


def write_msg(obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode()
    sys.stdout.buffer.write(struct.pack("@I", len(body)))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


class Reassembler:
    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def feed(self, msg: dict) -> str | None:
        """Feed one relayData/relayDataChunk frame; return the full text when
        a chunked message completes (whole frames return immediately)."""
        if msg.get("type") == "relayData":
            return str(msg.get("data") or "")
        if msg.get("type") != "relayDataChunk":
            return None
        mid = str(msg.get("messageId"))
        entry = self._entries.setdefault(
            mid, {"count": int(msg.get("chunkCount", 0)), "parts": []})
        entry["parts"].append(str(msg.get("data") or ""))
        if len(entry["parts"]) >= entry["count"]:
            self._entries.pop(mid, None)
            return "".join(entry["parts"])
        return None


def send_frame(data: str, next_id: list) -> None:
    """Host → Chrome: split when oversized (native messaging caps ~1MB)."""
    n = -(-len(data) // CHUNK)
    if n <= 1:
        write_msg({"type": "relayData", "messageId": f"hc-{next_id[0]}",
                   "encoding": "utf8", "data": data})
        next_id[0] += 1
        return
    mid = f"hc-{next_id[0]}"
    next_id[0] += 1
    for i in range(n):
        write_msg({"type": "relayDataChunk", "messageId": mid,
                   "chunkIndex": i, "chunkCount": n, "encoding": "utf8",
                   "data": data[i * CHUNK:(i + 1) * CHUNK]})


async def run_bridge() -> None:
    import websockets

    port = read_port()
    if not port:
        write_msg({"error": "ginno-not-running"})
        return
    reasm = Reassembler()
    to_ws: asyncio.Queue[str] = asyncio.Queue()
    next_id = [1]

    async def pump_ws() -> None:
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/extension/v2",
                max_size=64 * 1024 * 1024,
            ) as ws:
                write_msg({"type": "relayConnected", "port": port})

                async def reader() -> None:
                    async for raw in ws:
                        send_frame(raw.decode("utf-8", "replace"), next_id)

                async def writer() -> None:
                    while True:
                        frame = await to_ws.get()
                        await ws.send(frame)

                await asyncio.gather(reader(), writer())
        except Exception as e:  # noqa: BLE001 — 报错给扩展后退出
            write_msg({"error": f"bridge failed: {e}"})

    async def pump_stdin() -> None:
        loop = asyncio.get_running_loop()
        while True:
            msg = await loop.run_in_executor(None, read_msg)
            if msg is None:
                return  # Chrome closed the pipe → let bridge end
            mtype = msg.get("type")
            if mtype == "getPort":
                p = read_port()
                write_msg({"port": p} if p else {"error": "ginno-not-running"})
            elif mtype == "ping":
                write_msg({"pong": True})
            else:
                text = reasm.feed(msg)
                if text is not None:
                    await to_ws.put(text)

    await asyncio.gather(pump_ws(), pump_stdin())


def main() -> None:
    """Stdio loop: getPort/ping answered inline; relayConnect switches to the
    WS bridge loop until stdin closes."""
    while True:
        msg = read_msg()
        if msg is None:
            return
        mtype = msg.get("type")
        if mtype == "getPort":
            port = read_port()
            write_msg({"port": port} if port else {"error": "ginno-not-running"})
        elif mtype == "ping":
            write_msg({"pong": True})
        elif mtype == "relayConnect":
            asyncio.run(run_bridge())
            return
