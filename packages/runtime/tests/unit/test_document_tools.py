"""Unit tests: parse_document tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime.tools.document_tools import parse_document

pytestmark = pytest.mark.unit


def make_csv(p: Path) -> Path:
    p.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
    return p


def make_docx(p: Path) -> Path:
    from docx import Document

    doc = Document()
    doc.core_properties.author = "张三"
    doc.add_paragraph("协议内容")
    doc.save(p)
    return p


# --------------------------------------------------------------------------- #
# parse_document
# --------------------------------------------------------------------------- #

def test_parse_document_text(tmp_path):
    f = make_docx(tmp_path / "c.docx")
    out = parse_document.invoke({"path": str(f)})
    assert "协议内容" in out


def test_parse_document_metadata(tmp_path):
    f = make_docx(tmp_path / "c.docx")
    d = json.loads(parse_document.invoke({"path": str(f), "format": "metadata"}))
    assert d["ok"] is True
    assert d["kind"] == "document"
    assert d["metadata"]["author"] == "张三"


def test_parse_document_json(tmp_path):
    f = make_csv(tmp_path / "t.csv")
    d = json.loads(parse_document.invoke({"path": str(f), "format": "json"}))
    assert d["ok"] is True and d["kind"] == "table"
    assert "| a | b |" in d["text"]


def test_parse_document_errors_are_json(tmp_path):
    d = json.loads(parse_document.invoke({"path": str(tmp_path / "nope.xlsx")}))
    assert d["ok"] is False
    f = tmp_path / "x.exe"
    f.write_bytes(b"MZ")
    d2 = json.loads(parse_document.invoke({"path": str(f)}))
    assert d2["ok"] is False and "Unsupported" in d2["error"]


# ---- 载入行数帽（2026-10-17）：输出有帽、载入也有帽 ----------------------------
def test_table_load_capped(monkeypatch, tmp_path):
    """CSV 超过 LOAD_MAX_ROWS：只载入帽内行数，markdown 带 truncation 注记，
    metadata 标 capped——不再「整表进内存才裁 50 行」。"""
    from ginno_runtime.files import extractors as ex

    monkeypatch.setattr(ex, "LOAD_MAX_ROWS", 10)
    p = tmp_path / "t.csv"
    p.write_text("a,b\n" + "".join(f"{i},{i * 2}\n" for i in range(30)), encoding="utf-8")
    out = ex.extract(p)
    assert "loaded first 10 rows" in out.markdown
    sheet = out.metadata["sheets"][0]
    assert sheet["rows"] == 10
    assert sheet["capped"] is True


def test_xlsx_load_capped(monkeypatch, tmp_path):
    """xlsx 同帽生效（read_excel nrows 落到引擎层）。"""
    pandas = pytest.importorskip("pandas")
    from ginno_runtime.files import extractors as ex

    monkeypatch.setattr(ex, "LOAD_MAX_ROWS", 5)
    p = tmp_path / "s.xlsx"
    pandas.DataFrame({"a": range(20), "b": range(20)}).to_excel(p, index=False)
    out = ex.extract(p)
    assert "loaded first 5 rows" in out.markdown
    assert out.metadata["sheets"][0]["capped"] is True


def test_read_table_below_cap_untouched(monkeypatch, tmp_path):
    """帽下文件行为不变：无注记、capped=False、行数如实。"""
    from ginno_runtime.files import extractors as ex

    monkeypatch.setattr(ex, "LOAD_MAX_ROWS", 10)
    p = tmp_path / "t.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    out = ex.extract(p)
    assert "仅载入前" not in out.markdown
    sheet = out.metadata["sheets"][0]
    assert sheet["rows"] == 2 and sheet["capped"] is False
