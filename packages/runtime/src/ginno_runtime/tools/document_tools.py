"""Document tools — parse_document.

``parse_document`` extracts any supported file to markdown/json/metadata
(via ``files.extractors``).

Numeric questions about tables are answered with the ``bash`` tool (the agent
writes pandas code there): there is no separate ``analyze_table`` tool — a
dedicated isolated-subprocess runner was more surface area than it was worth
once ``bash`` could do the same job. See docs/file-parsing-research.md.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from ..files import extractors as ex
from ..lang import t


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
    JSON on success. For numeric questions about a table, read the file with
    ``bash`` (e.g. pandas) instead of pasting the whole table.
    """
    try:
        res = ex.extract(path)
    except (FileNotFoundError, ex.UnsupportedFormat, ex.ExtractorUnavailable) as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps(
            {"ok": False, "error": t(f"Parse failed: {type(e).__name__}: {e}",
                                     f"解析失败: {type(e).__name__}: {e}")},
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


ALL_DOCUMENT_TOOLS = [parse_document]