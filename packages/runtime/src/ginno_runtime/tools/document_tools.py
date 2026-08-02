"""Document tools — parse_document / analyze_table.

``parse_document`` extracts any supported file to markdown/json/metadata
(via ``files.extractors``). ``analyze_table`` lets the agent answer data
questions by *writing pandas code* that runs in an isolated subprocess
(``python -I`` + timeout): the model sees a schema summary, not the whole
table, and gets back only the result (see docs/file-parsing-research.md §4.2).

When an analysis yields a table result, it is written to a derived CSV next
to the source file and the path is returned in the JSON output — the WS
layer (server.py) turns that into an artifact + ``preview.emit`` so the
result sheet opens automatically in the UI.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

from langchain_core.tools import tool

from ..files import extractors as ex

DOCUMENT_TOOL_NAMES = {"parse_document", "analyze_table"}

DERIVED_MAX_ROWS = 5000
_STDERR_LIMIT = 800


@tool
def parse_document(path: str, format: str = "text") -> str:
    """Parse a document/spreadsheet file into readable content.

    Supports: xlsx/xls/xlsm, csv/tsv, docx, pptx, pdf, json, xml, txt, md.

    Args:
        path: absolute path (or workspace-relative) to the file.
        format: "text" (default) → markdown body; "json" → full structure
            {kind, metadata, text}; "metadata" → metadata only (author,
            title, sheet list, page count, ...). Fastest for "who wrote this".

    Returns JSON on errors ({"ok": false, "error": ...}); plain markdown or
    JSON on success. Prefer analyze_table for numeric questions about tables.
    """
    try:
        res = ex.extract(path)
    except (FileNotFoundError, ex.UnsupportedFormat, ex.ExtractorUnavailable) as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps(
            {"ok": False, "error": f"解析失败: {type(e).__name__}: {e}"},
            ensure_ascii=False,
        )
    fmt = (format or "text").lower()
    if fmt == "metadata":
        return json.dumps(
            {"ok": True, "kind": res.kind, "metadata": res.metadata},
            ensure_ascii=False,
        )
    if fmt == "json":
        return json.dumps(
            {"ok": True, "kind": res.kind, "metadata": res.metadata, "text": res.markdown},
            ensure_ascii=False,
        )
    return res.markdown


_ANALYZE_TEMPLATE = """\
import json as _json
import sys as _sys
import pandas as pd

_path = {path!r}
_sheet = {sheet!r} or None
try:
    if _path.lower().endswith(".tsv"):
        df = pd.read_csv(_path, sep="\\t")
    elif _path.lower().endswith(".csv"):
        df = pd.read_csv(_path)
    else:
        try:
            _frames = pd.read_excel(_path, engine="calamine", sheet_name=_sheet)
        except ImportError:
            _frames = pd.read_excel(_path, sheet_name=_sheet)
        if isinstance(_frames, dict):
            df = next(iter(_frames.values()))
        else:
            df = _frames
except Exception as _e:
    print(_json.dumps({{"ok": False,
                       "error": f"load failed: {{type(_e).__name__}}: {{_e}}"}},
                      ensure_ascii=False))
    _sys.exit(0)

result = None
{code}

def _cell(v):
    if pd.isna(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v

try:
    if isinstance(result, pd.DataFrame):
        _res = {{
            "type": "table",
            "columns": [str(c) for c in result.columns],
            "rows": [[_cell(v) for v in row] for _, row in result.head(100).iterrows()],
            "shape": list(result.shape),
        }}
        _rtype = "table"
    else:
        _res = _cell(result) if not isinstance(result, (list, dict)) else result
        _rtype = type(result).__name__
    print(_json.dumps({{"ok": True, "result_type": _rtype, "result": _res}},
                      ensure_ascii=False, default=str))
except Exception as _e:
    print(_json.dumps({{"ok": False, "error": f"result serialize failed: {{_e}}"}},
                      ensure_ascii=False))
"""


def _run_analysis(path: str, code: str, sheet: str, timeout: int) -> dict:
    script = _ANALYZE_TEMPLATE.format(
        path=path, sheet=sheet or "", code=textwrap.dedent(code)
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", script],  # -I: isolated (no env/site)
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（>{timeout}s）"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parsed: dict | None = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if parsed is None:
        return {
            "ok": False,
            "error": "代码未产出可解析结果",
            "stderr": err[-_STDERR_LIMIT:],
        }
    if err:
        parsed["stderr"] = err[-_STDERR_LIMIT:]
    return parsed


def _write_derived(source: Path, result: dict) -> str | None:
    """Persist a table result as CSV next to the source; returns the path."""
    if result.get("result_type") != "table":
        return None
    rows = result.get("result") or {}
    shape = rows.get("shape") or [0, 0]
    if not shape[0] or shape[0] > DERIVED_MAX_ROWS:
        return None
    try:
        import pandas as pd

        df = pd.DataFrame(rows.get("rows") or [], columns=rows.get("columns") or [])
        dest_dir = source.parent / "results"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{source.stem}-result-{uuid.uuid4().hex[:8]}.csv"
        df.to_csv(dest, index=False)
        return str(dest)
    except Exception:
        return None


@tool
def analyze_table(path: str, code: str, sheet: str = "", timeout: int = 30) -> str:
    """Answer data questions about a CSV/Excel file by running pandas code.

    The file is loaded as DataFrame ``df`` (use ``sheet`` to pick a sheet).
    Write code that assigns the answer to ``result`` — a scalar, list, or a
    DataFrame for tabular answers. Only the result is returned, never the
    raw data, so this works on huge files.

    Example: code="result = df.groupby('地区')['金额'].sum().sort_values(ascending=False).head(10)"

    Runs in an isolated subprocess with a timeout. A DataFrame result is also
    saved as a derived CSV (path in "derived_path") so it can be previewed.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return json.dumps({"ok": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)
    if ex.classify(p) not in ("spreadsheet", "table"):
        return json.dumps(
            {"ok": False, "error": f"analyze_table 仅支持 csv/xlsx 等表格文件: {p.suffix}"},
            ensure_ascii=False,
        )
    res = _run_analysis(str(p), code, sheet, max(1, min(timeout, 120)))
    if res.get("ok") and res.get("result_type") == "table":
        derived = _write_derived(p, res)
        if derived:
            res["derived_path"] = derived
    return json.dumps(res, ensure_ascii=False, default=str)


ALL_DOCUMENT_TOOLS = [parse_document, analyze_table]
