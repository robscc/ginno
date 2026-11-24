"""Unit tests: preview payloads (paginated grids + document markdown)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from ginno_runtime.files import extractors as ex
from ginno_runtime.files import preview
from ginno_runtime.files.preview import build_csv_export, build_preview

pytestmark = pytest.mark.unit


def make_big_xlsx(p: Path, rows: int = 150) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(["id", "value"])
    for i in range(rows):
        ws.append([i, i * 1.5])
    ws2 = wb.create_sheet("other")
    ws2.append(["x"])
    wb.save(p)
    return p


def test_spreadsheet_preview_pagination(tmp_path):
    f = make_big_xlsx(tmp_path / "big.xlsx", rows=150)
    pv = build_preview(f, offset=100, limit=100)
    assert pv["kind"] == "spreadsheet"
    assert pv["sheet"] == "data"  # first sheet by default
    assert {s["name"] for s in pv["sheets"]} == {"data", "other"}
    assert pv["total_rows"] == 150
    assert len(pv["rows"]) == 50  # only 50 left after offset 100
    # cells are stringified; xlsx stores numbers as float, so accept "100"/"100.0"
    assert float(pv["rows"][0][0]) == 100
    assert pv["rows"][0][1] == "150.0"
    assert {c["name"] for c in pv["columns"]} == {"id", "value"}


def test_spreadsheet_preview_sheet_select_and_clamp(tmp_path, monkeypatch):
    f = make_big_xlsx(tmp_path / "big.xlsx")
    calls = []
    read_spreadsheet = ex._read_spreadsheet

    def counted(path):
        calls.append(path)
        return read_spreadsheet(path)

    monkeypatch.setattr(ex, "_read_spreadsheet", counted)
    pv = build_preview(f, sheet="other")
    assert pv["sheet"] == "other"
    assert pv["total_rows"] == 0
    # bogus sheet falls back to the first
    pv2 = build_preview(f, sheet="nope")
    assert pv2["sheet"] == "data"
    # limit clamps into [1, 500]
    pv3 = build_preview(f, limit=99999)
    assert pv3["limit"] == 500
    pv4 = build_preview(f, limit=0)
    assert pv4["limit"] == 1
    assert calls == [f]


@pytest.fixture
def table_parse_calls(monkeypatch):
    calls = []
    read_table = ex.read_table

    def counted(path):
        calls.append(path)
        return read_table(path)

    monkeypatch.setattr(ex, "read_table", counted)
    return calls


def test_table_preview_invalidates_changed_mtime_and_replaced_source(tmp_path, table_parse_calls):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    assert build_preview(path)["rows"] == [["1"]]
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    assert build_preview(path)["rows"] == [["1"]]
    assert table_parse_calls == [path, path]

    old = path.stat()
    replacement = tmp_path / "replacement.csv"
    replacement.write_text("value\n2\n", encoding="utf-8")
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    replacement.replace(path)
    new = path.stat()
    assert (old.st_size, old.st_mtime_ns) == (new.st_size, new.st_mtime_ns)
    assert build_preview(path)["rows"] == [["2"]]
    assert build_preview(path)["rows"] == [["2"]]
    assert table_parse_calls == [path, path, path]


def test_table_preview_does_not_cache_read_when_source_changes(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    read_table = ex.read_table
    calls = []

    def read_then_replace(path):
        frame = read_table(path)
        calls.append(path)
        if len(calls) == 1:
            path.write_text("value\n22\n", encoding="utf-8")
        return frame

    monkeypatch.setattr(ex, "read_table", read_then_replace)
    assert build_preview(path)["rows"] == [["1"]]
    assert path.absolute() not in preview._TABLE_CACHE
    assert build_preview(path)["rows"] == [["22"]]
    assert build_preview(path)["rows"] == [["22"]]
    assert calls == [path, path]


def test_table_preview_distinguishes_paths_to_same_source(tmp_path, table_parse_calls):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    alias = tmp_path / "alias.csv"
    alias.hardlink_to(path)
    assert build_preview(path)["sheet"] == "data"
    assert build_preview(alias)["sheet"] == "alias"
    assert build_preview(path)["sheet"] == "data"
    assert table_parse_calls == [path, alias]


def test_table_preview_over_budget_still_returns_data(tmp_path, monkeypatch, table_parse_calls):
    path = tmp_path / "data.csv"
    path.write_text("value\n123\n", encoding="utf-8")
    monkeypatch.setattr(preview, "_TABLE_CACHE_MAX_BYTES", 1)
    for _ in range(2):
        assert build_preview(path)["rows"] == [["123"]]
    assert table_parse_calls == [path, path]


@pytest.mark.parametrize("limit", ["_TABLE_CACHE_MAX_ENTRIES", "_TABLE_CACHE_MAX_BYTES"])
def test_table_preview_evicts_least_recent_entry(tmp_path, monkeypatch, table_parse_calls, limit):
    paths = [tmp_path / f"table{i}.csv" for i in range(3)]
    for path in paths:
        path.write_text("n\n1\n", encoding="utf-8")
    frame = ex.read_table(paths[0])
    frame_bytes = int(frame.memory_usage(deep=True).sum() + frame.columns.memory_usage(deep=True))
    table_parse_calls.clear()
    monkeypatch.setattr(preview, limit, 2 if limit.endswith("ENTRIES") else 2 * frame_bytes)
    first, second, third = paths
    for path in (first, second, first, third, first, second):
        assert build_preview(path)["rows"] == [["1"]]
    assert table_parse_calls == [first, second, third, second]


def test_table_preview_parallel_pages_share_parse(tmp_path, table_parse_calls):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n2\n", encoding="utf-8")
    start = Barrier(2, timeout=5)

    def load(offset):
        start.wait()
        return build_preview(path, offset=offset, limit=1)["rows"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(load, (0, 1))) == [[["1"]], [["2"]]]
    assert table_parse_calls == [path]


def test_table_preview_retries_parse_failure(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    read_table = ex.read_table
    calls = []

    def fail_once(path):
        calls.append(path)
        if len(calls) == 1:
            raise ValueError("transient parse failure")
        return read_table(path)

    monkeypatch.setattr(ex, "read_table", fail_once)
    with pytest.raises(ValueError, match="transient parse failure"):
        build_preview(path)
    assert build_preview(path)["rows"] == [["1"]]
    assert build_preview(path)["rows"] == [["1"]]
    assert calls == [path, path]


def test_csv_preview(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    pv = build_preview(f)
    assert pv["kind"] == "table"
    assert pv["rows"] == [["1", "2"]]
    assert pv["total_rows"] == 1


def test_csv_preview_tolerates_bom_and_ragged_rows(tmp_path):
    # Real-world Excel CSV: UTF-8 BOM + a row with one extra comma. Previously
    # raised ParserError ("Expected N fields … saw N+1") and the whole preview
    # failed, while xlsx (calamine) was fine.
    f = tmp_path / "数据分析.csv"
    f.write_bytes("cnt,from,sum\n1,a,10\n2,b,20,EXTRA\n3,c,30\n".encode("utf-8-sig"))
    pv = build_preview(f)
    assert pv["kind"] == "table"
    # BOM stripped from the first column name
    assert [c["name"] for c in pv["columns"]] == ["cnt", "from", "sum"]
    # the ragged row is skipped, the rest survive
    assert pv["total_rows"] == 2
    assert pv["rows"] == [["1", "a", "10"], ["3", "c", "30"]]
    # extract (parse_document) and csv export are equally tolerant
    assert ex.read_table(f).shape == (2, 3)
    _, data = build_csv_export(f)
    assert data.decode("utf-8-sig").splitlines()[0] == "cnt,from,sum"


def test_document_preview_markdown(tmp_path):
    from docx import Document

    f = tmp_path / "d.docx"
    doc = Document()
    doc.add_heading("标题", level=1)
    doc.add_paragraph("正文段落")
    doc.save(f)
    pv = build_preview(f)
    assert pv["kind"] == "document"
    assert "正文段落" in pv["markdown"]
    assert "metadata" in pv


def test_unsupported_preview(tmp_path):
    f = tmp_path / "x.exe"
    f.write_bytes(b"MZ")
    with pytest.raises(ex.UnsupportedFormat):
        build_preview(f)


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

def test_csv_export_roundtrip(tmp_path):
    f = tmp_path / "数据.csv"
    f.write_text("名称,金额\n甲,1.5\n乙,2\n", encoding="utf-8")
    name, data = build_csv_export(f)
    assert name == "数据.csv"
    text = data.decode("utf-8-sig")  # BOM so Excel opens Chinese correctly
    assert data.startswith(b"\xef\xbb\xbf")
    assert "名称,金额" in text and "甲,1.5" in text


def test_csv_export_xlsx_sheet_select(tmp_path):
    f = make_big_xlsx(tmp_path / "big.xlsx")  # sheets: data (150), other (0)
    name, data = build_csv_export(f)
    assert name == "big-data.csv"  # multi-sheet → suffixed with active sheet
    lines = data.decode("utf-8-sig").strip().splitlines()
    assert lines[0] == "id,value"
    assert len(lines) == 151  # header + 150 rows
    name2, data2 = build_csv_export(f, sheet="other")
    assert name2 == "big-other.csv"
    assert data2.decode("utf-8-sig").strip() == "x"  # header only


def test_csv_export_tsv_converts_to_commas(tmp_path):
    f = tmp_path / "t.tsv"
    f.write_text("a\tb\n1\t2\n", encoding="utf-8")
    name, data = build_csv_export(f)
    assert name == "t.csv"
    assert "a,b" in data.decode("utf-8-sig")


def test_csv_export_rejects_non_table(tmp_path):
    from docx import Document

    f = tmp_path / "d.docx"
    doc = Document()
    doc.add_paragraph("x")
    doc.save(f)
    with pytest.raises(ex.UnsupportedFormat):
        build_csv_export(f)


def test_csv_export_name_override(tmp_path):
    # uploads are stored with a uuid prefix; the display name must win
    f = tmp_path / "8a24fbf7-报表.csv"
    f.write_text("a\n1\n", encoding="utf-8")
    name, _ = build_csv_export(f, name="报表.csv")
    assert name == "报表.csv"


def test_drawio_classified_and_previewable(tmp_path):
    # .drawio/.dio → kind "drawio"; build_preview mirrors the html pattern
    # (no extraction, frontend renders with the embedded GraphViewer).
    xml = '<mxfile><diagram><mxGraphModel><root><mxCell id="0"/></root></mxGraphModel></diagram></mxfile>'
    for name in ("a.drawio", "b.dio"):
        f = tmp_path / name
        f.write_text(xml, encoding="utf-8")
        assert ex.classify(f) == "drawio"
        assert build_preview(f) == {"kind": "drawio"}
