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
    assert d2["ok"] is False and "不支持" in d2["error"]
