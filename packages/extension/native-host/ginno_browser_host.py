#!/usr/bin/env python3
"""Ginno native messaging host — standalone shim.

The protocol implementation lives in ``ginno_runtime.browser.host_protocol``
(single importable copy, shared with the frozen binary's --native-host mode).
The shebang is rewritten on install to the sidecar's interpreter, which has
ginno_runtime importable.

If the import fails (bare python), fall back to port discovery only — enough
for the extension to show a helpful error instead of failing silently.
"""

from __future__ import annotations

import json
import struct
import sys


def _read() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("@I", raw)
    try:
        return json.loads(sys.stdin.buffer.read(n))
    except (ValueError, TypeError):
        return {}


def _write(obj: dict) -> None:
    body = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("@I", len(body)))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def main() -> None:
    try:
        from ginno_runtime.browser import host_protocol

        host_protocol.main()
        return
    except ImportError:
        pass
    # degraded responder: port discovery only
    import os
    from pathlib import Path

    home = os.environ.get("GINNO_HOME")
    port_file = Path(home or "~/.ginno").expanduser() / "browser-relay-port"
    while True:
        msg = _read()
        if msg is None:
            return
        if msg.get("type") == "getPort":
            try:
                _write({"port": int(port_file.read_text().strip())})
            except (OSError, ValueError):
                _write({"error": "ginno-not-running"})
        elif msg.get("type") == "ping":
            _write({"pong": True})
        elif msg.get("type") == "relayConnect":
            _write({"error": "host protocol unavailable (ginno_runtime missing)"})


if __name__ == "__main__":
    main()
