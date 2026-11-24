"""Preview payloads for the UI.

Spreadsheets/tables → paginated grid JSON (sheet tabs, columns+dtypes, a
page of stringified rows). Documents/presentations/PDFs → extracted
markdown (rendered by the existing markdown viewer). Everything routes
through :mod:`extractors` so lazy deps + graceful degrade apply.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import Lock

from ..lang import t
from . import extractors as ex

MAX_LIMIT = 500


_TABLE_CACHE_MAX_ENTRIES = 16
_TABLE_CACHE_MAX_BYTES = 64 * 1024 * 1024
_FileVersion = tuple[int, int, int, int, int]
_TABLE_CACHE: OrderedDict[Path, tuple[_FileVersion, dict, int]] = OrderedDict()
_TABLE_CACHE_LOCK = Lock()
# Fixed stripes bound lock memory while coalescing simultaneous misses for a path.
_TABLE_PARSE_LOCKS = tuple(Lock() for _ in range(32))


def _file_version(path: Path) -> _FileVersion | None:
    try:
        stat = path.stat()
    except OSError:
        return None  # let the extractor report its usual error
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _load_table_frames(path: Path, kind: str) -> dict:
    if kind == "spreadsheet":
        _, frames = ex._read_spreadsheet(path)
        return frames if isinstance(frames, dict) else {path.stem: frames}
    return {path.stem: ex.read_table(path)}


def _table_frames(path: Path, kind: str) -> dict:
    key = path.absolute()
    with _TABLE_PARSE_LOCKS[hash(key) % len(_TABLE_PARSE_LOCKS)]:
        version = _file_version(path)
        with _TABLE_CACHE_LOCK:
            cached = _TABLE_CACHE.pop(key, None)
            if cached is not None and cached[0] == version:
                _TABLE_CACHE[key] = cached
                return cached[1]

        frames = _load_table_frames(path, kind)
        if version is None:
            return frames
        size = sum(
            int(frame.memory_usage(deep=True).sum()) + int(frame.columns.memory_usage(deep=True))
            for frame in frames.values()
        )
        # A file replaced or edited during parsing must not populate the cache.
        if size <= _TABLE_CACHE_MAX_BYTES and _file_version(path) == version:
            with _TABLE_CACHE_LOCK:
                _TABLE_CACHE[key] = (version, frames, size)
                total = sum(item[2] for item in _TABLE_CACHE.values())
                while len(_TABLE_CACHE) > _TABLE_CACHE_MAX_ENTRIES or total > _TABLE_CACHE_MAX_BYTES:
                    _, (_, _, evicted_size) = _TABLE_CACHE.popitem(last=False)
                    total -= evicted_size
        return frames


def _table_payload(path: Path, kind: str, sheet: str | None, offset: int, limit: int) -> dict:
    frames = _table_frames(path, kind)

    names = list(frames.keys())
    active = sheet if sheet in frames else names[0]
    df = frames[active]
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    page = df.iloc[offset : offset + limit]
    return {
        "kind": kind,
        "sheets": [
            {
                "name": str(n),
                "rows": int(len(frames[n])),
                "cols": int(len(frames[n].columns)),
            }
            for n in names
        ],
        "sheet": str(active),
        "columns": [
            {"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns
        ],
        "rows": [[ex._cell(v) for v in row] for _, row in page.iterrows()],
        "total_rows": int(len(df)),
        "offset": offset,
        "limit": limit,
    }


def build_preview(
    path: str | Path,
    sheet: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Build a preview payload for any supported file.

    Tables: paginated grid. Documents: ``{"markdown", "metadata"}``.
    Raises ``extractors.UnsupportedFormat`` / ``ExtractorUnavailable``.
    """
    p = Path(path).expanduser()
    kind = ex.classify(p)
    if kind == "html":
        # No extraction: the frontend streams the bytes into a sandboxed
        # iframe via GET /api/files/{id}/inline (same CSP as code panel).
        return {"kind": "html"}
    if kind == "drawio":
        # No extraction either: the frontend fetches the raw XML via the
        # download URL and renders it with the vendored diagrams.net
        # GraphViewer inside a sandboxed srcdoc iframe (same double-sandbox
        # idea as html, but client-assembled so the viewer script is served
        # from the web bundle instead of the sidecar).
        return {"kind": "drawio"}
    if kind in ("spreadsheet", "table"):
        return _table_payload(p, kind, sheet, offset, limit)
    if kind in ("document", "presentation", "pdf", "data", "text"):
        res = ex.extract(p)
        return {"kind": kind, "markdown": res.markdown, "metadata": res.metadata}
    raise ex.UnsupportedFormat(t(f"Unsupported file format for preview: {p.suffix}", f"不支持预览的文件格式: {p.suffix}"))


def build_csv_export(
    path: str | Path, sheet: str | None = None, name: str | None = None
) -> tuple[str, bytes]:
    """Export a table/spreadsheet (or one sheet of it) as CSV.

    Returns ``(suggested_filename, csv_bytes)``. Bytes are ``utf-8-sig``
    (BOM) so Excel renders Chinese characters correctly on open. A
    multi-sheet workbook exports the selected sheet (default: first) and
    the filename is suffixed with the sheet name; TSV input is converted
    to comma-separated output. ``name`` overrides the filename stem —
    callers pass the registry's display name so exports aren't labelled
    with the storage path's uuid prefix. Raises
    ``extractors.UnsupportedFormat`` for non-table kinds,
    ``ExtractorUnavailable`` when pandas is missing.
    """
    p = Path(path).expanduser()
    kind = ex.classify(p)
    stem = Path(name).stem if name else p.stem
    if kind == "spreadsheet":
        _, frames = ex._read_spreadsheet(p)
        if not isinstance(frames, dict):
            frames = {p.stem: frames}
        names = list(frames.keys())
        active = sheet if sheet in frames else names[0]
        df = frames[active]
        out_name = f"{stem}-{active}.csv" if len(names) > 1 else f"{stem}.csv"
    elif kind == "table":
        df = ex.read_table(p)
        out_name = f"{stem}.csv"
    else:
        raise ex.UnsupportedFormat(t(f"CSV export only supports table-like files: {p.suffix}", f"CSV 导出仅支持表格类文件: {p.suffix}"))
    return out_name, df.to_csv(index=False).encode("utf-8-sig")
