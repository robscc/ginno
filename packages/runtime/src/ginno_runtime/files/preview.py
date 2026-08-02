"""Preview payloads for the UI.

Spreadsheets/tables → paginated grid JSON (sheet tabs, columns+dtypes, a
page of stringified rows). Documents/presentations/PDFs → extracted
markdown (rendered by the existing markdown viewer). Everything routes
through :mod:`extractors` so lazy deps + graceful degrade apply.
"""

from __future__ import annotations

from pathlib import Path

from . import extractors as ex

MAX_LIMIT = 500


def _table_payload(path: Path, kind: str, sheet: str | None, offset: int, limit: int) -> dict:
    if kind == "spreadsheet":
        _, frames = ex._read_spreadsheet(path)
        if not isinstance(frames, dict):
            frames = {path.stem: frames}
    elif kind == "table":
        pd = ex._require("pandas", "CSV")
        sep = "\t" if path.suffix.lower() == ".tsv" else ","
        frames = {path.stem: pd.read_csv(path, sep=sep)}
    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"not a table kind: {kind}")

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
    if kind in ("spreadsheet", "table"):
        return _table_payload(p, kind, sheet, offset, limit)
    if kind in ("document", "presentation", "pdf", "data", "text"):
        res = ex.extract(p)
        return {"kind": kind, "markdown": res.markdown, "metadata": res.metadata}
    raise ex.UnsupportedFormat(f"不支持预览的文件格式: {p.suffix}")


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
        pd = ex._require("pandas", "CSV")
        sep = "\t" if p.suffix.lower() == ".tsv" else ","
        df = pd.read_csv(p, sep=sep)
        out_name = f"{stem}.csv"
    else:
        raise ex.UnsupportedFormat(f"CSV 导出仅支持表格类文件: {p.suffix}")
    return out_name, df.to_csv(index=False).encode("utf-8-sig")
