"""Atomic snapshots for stores using the runtime JSON format."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Any

from ginno_runtime import jsonx as json


def write_json(path: Path, data: Any, *, preserve_mode: bool = False) -> None:
    """Replace a JSON file in place after closing a complete same-directory snapshot.

    The caller owns parent creation and any read/modify/write lock. New files
    use normal text-file permissions (0666 filtered by the umask).
    """
    text = json.dumps(data, indent=2, ensure_ascii=False)
    mode = None
    if preserve_mode:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass

    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the original write or replace exception.
        raise
