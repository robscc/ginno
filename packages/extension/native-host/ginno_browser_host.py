#!/usr/bin/env python3
"""Ginno native messaging host — tells the extension which port the sidecar
relay listens on (M3 port discovery, browser-companion design §7.3).

Chrome spawns this via the manifest in
~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
(written by the sidecar: ginno_runtime/browser/native_host.py).
Protocol: length-prefixed JSON on stdin/stdout. Reads the port from
~/.ginno/browser-relay-port (written by the sidecar at startup). Responds to
{"type":"getPort"} with {"port": <int>}; exits when stdin closes.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path


def port_file() -> Path:
    home = os.environ.get("GINNO_HOME")
    return Path(home or "~/.ginno").expanduser() / "browser-relay-port"


def read_msg() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("@I", raw)
    body = sys.stdin.buffer.read(n)
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return {}


def write_msg(obj: dict) -> None:
    body = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("@I", len(body)))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        msg = read_msg()
        if msg is None:
            return
        if msg.get("type") == "getPort":
            port = None
            try:
                port = int(port_file().read_text().strip())
            except (OSError, ValueError):
                port = None
            write_msg({"port": port} if port else {"error": "ginno-not-running"})
        elif msg.get("type") == "ping":
            write_msg({"pong": True})


if __name__ == "__main__":
    main()
