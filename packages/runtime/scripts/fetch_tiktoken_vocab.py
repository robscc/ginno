"""Vendor the o200k_base BPE vocab for offline desktop builds.

tiktoken normally downloads ``o200k_base.tiktoken`` from openaipublic on first
use. The desktop app must work offline, so the Makefile runs this script before
PyInstaller and bundles the file under ``tiktoken_cache/<sha1-of-url>`` — the
exact cache filename tiktoken's ``read_file_cached`` looks for.

Best-effort: a failed download prints a warning and exits 0; the panel then
falls back to the char estimator (``token_count.counter_name() == "estimate"``).
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
CACHE_NAME = hashlib.sha1(URL.encode()).hexdigest()
DEST = Path(__file__).resolve().parents[1] / "vendor" / "tiktoken" / CACHE_NAME


def main() -> int:
    if DEST.is_file() and DEST.stat().st_size > 1_000_000:
        print(f"tiktoken vocab present: {DEST}")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(URL, timeout=60) as resp:
            data = resp.read()
        if len(data) < 1_000_000:
            raise RuntimeError(f"unexpected vocab size: {len(data)} bytes")
        tmp = DEST.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(DEST)
        print(f"tiktoken vocab fetched: {DEST} ({len(data)} bytes)")
    except Exception as exc:  # noqa: BLE001 — offline build is allowed
        print(f"warning: tiktoken vocab fetch failed ({exc}); panel will estimate", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())