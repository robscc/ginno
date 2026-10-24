"""orjson-backed drop-in for the stdlib ``json`` module.

Runtime code (and the test suite) imports this as ``json``::

    from ginno_runtime import jsonx as json

so serialization goes through orjson — several times faster than stdlib on
both directions, which matters for the turn loop (WS frames, checkpoints,
usage ledgers, tool payloads). Call sites keep the stdlib spelling they were
written with; this module absorbs the API differences:

* ``dumps`` returns ``str`` (orjson returns ``bytes``).
* ``ensure_ascii`` is accepted and ignored: orjson always emits UTF-8, i.e.
  the ``ensure_ascii=False`` behaviour every call site already asks for.
* ``indent`` (any positive int or string) maps to ``OPT_INDENT_2`` — orjson
  only formats with two spaces. Generated files previously written with
  ``indent=1`` therefore gain one space per level; content is unchanged.
* ``sort_keys=True`` maps to ``OPT_SORT_KEYS``.
* ``separators`` is accepted and ignored: orjson's compact output is already
  the ``(",", ":")`` form the one caller uses, and there is no spaced form.
* Mapping keys that are not ``str`` need ``OPT_NON_STR_KEYS``; stdlib coerces
  them, so ``dumps`` retries with that flag when orjson raises ``TypeError``.
* ``JSONDecoder.raw_decode`` has no orjson equivalent — that one method
  delegates to stdlib lazily, for the truncated-payload scanner in
  ``api/server_tools.py``.
"""

from __future__ import annotations

from typing import IO, Any

import orjson

JSONDecodeError = orjson.JSONDecodeError

__all__ = [
    "JSONDecodeError",
    "JSONDecoder",
    "dump",
    "dumps",
    "load",
    "loads",
]


def _options(indent: Any, sort_keys: bool) -> int:
    option = 0
    if indent:
        option |= orjson.OPT_INDENT_2
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS
    return option


def dumps(
    obj: Any,
    *,
    default: Any = None,
    ensure_ascii: bool | None = None,
    indent: int | str | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    cls: Any = None,
    allow_nan: bool | None = None,
    skipkeys: bool = False,
    check_circular: bool | None = None,
) -> str:
    """Serialize ``obj`` to a JSON ``str`` (orjson semantics, stdlib spelling)."""
    if cls is not None:
        raise TypeError("jsonx.dumps does not support cls=; pass default= instead")
    option = _options(indent, sort_keys)
    try:
        return orjson.dumps(obj, default=default, option=option).decode()
    except TypeError:
        # stdlib stringifies int/float/bool/None mapping keys; orjson needs
        # the opt-in. Retry once so those payloads keep working.
        return orjson.dumps(
            obj, default=default, option=option | orjson.OPT_NON_STR_KEYS
        ).decode()


def dump(obj: Any, fp: IO[str], **kwargs: Any) -> None:
    """Serialize ``obj`` as JSON to the text file object ``fp``."""
    fp.write(dumps(obj, **kwargs))


def loads(s: str | bytes | bytearray | memoryview, **kwargs: Any) -> Any:
    """Deserialize JSON from ``s`` (str or UTF-8 bytes)."""
    return orjson.loads(s)


def load(fp: IO[str] | IO[bytes], **kwargs: Any) -> Any:
    """Deserialize JSON from the file object ``fp``."""
    return orjson.loads(fp.read())


class JSONDecoder:
    """Compatibility shim; only ``raw_decode`` is used in this codebase."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def decode(self, s: str) -> Any:
        return loads(s)

    def raw_decode(self, s: str, idx: int = 0) -> tuple[Any, int]:
        import json as _stdlib

        return _stdlib.JSONDecoder().raw_decode(s, idx)
