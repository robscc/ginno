"""Search for the code panel: ``GET /api/code/search`` (brief §3.1, design §4.6).

Every test here runs against a REAL temporary tree; nothing is mocked except
the one walk budget that cannot be reached cheaply (see the truncation test).
That matters most for the regression this file exists for:

* **Heavy dirs must be pruned in CONTENT search.** ``grep_files`` (whose
  implementation the design tells us to reuse) only prunes ``_path_denied``,
  which does NOT exclude ``node_modules``; a naive port therefore spends the
  whole 20000-dir budget inside it and returns noise. The content test plants a
  string that is *guaranteed* to match inside ``node_modules`` / ``.git`` /
  ``.next`` and asserts it is absent from the hits — and, separately, reads the
  planted file back to prove it really does contain the token (so the absence
  is the prune working, not the fixture being wrong). Deleting the prune turns
  this test red; that mutation is recorded in the task report.
* **Truncation is explicit.** A scan that stops early must set ``truncated``
  and report ``scanned``, or the UI reads a partial result as "searched
  everything" (brief §1-2). Both budgets are exercised: the content hit cap
  with a real 600-line file, and the name entry cap with a small patched
  budget over a real 20-file directory.

Sessions are warm (``_SESSIONS``) like ``test_code_write.py``; ``isolated_home``
(tests/conftest.py, autouse) points ``$GINNO_HOME`` at a fresh tmp dir, and
``tmp_path_factory`` provides the mount dirs outside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-search"
Q = {"project_slug": SLUG, "session_id": SID}

#: A literal that must appear ONLY where we plant it, so a hit proves the file
#: was actually read and a miss proves it was skipped.
TOKEN = "NEEDLE_TOKEN_XYZ"
#: Non-ASCII content for the GBK probe test (grep_files' utf-8-only read would
#: silently miss this file).
GBK_WORD = "中文检视标记"


def _write(p: Path, text: str, encoding: str = "utf-8") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding=encoding)
    return p


@pytest.fixture
def panel(tmp_path_factory):
    """A warm session over three real trees: the main one, a bulk one, a gone one.

    ``f_rw`` is the standard tree every search test uses, ``f_bulk`` holds the
    many-file directory the name-truncation test needs (kept separate so the
    main tree stays small and predictable), and ``f_gone`` points at a removed
    directory so ``root-missing`` is exercised (§4.3 codes).
    """
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    rw = tmp_path_factory.mktemp("rw-mount")
    bulk = tmp_path_factory.mktemp("bulk-mount")
    gone = tmp_path_factory.mktemp("gone-mount")
    gone.rmdir()  # the mount still points here → root-missing, not unknown-root

    _seed(rw, ws)

    _SESSIONS[SID] = {
        "project_slug": SLUG,
        "context_dirs": [
            {"id": "f_rw", "path": str(rw), "name": "rw", "access": "rw"},
            {"id": "f_bulk", "path": str(bulk), "name": "bulk", "access": "rw"},
            {"id": "f_gone", "path": str(gone), "name": "gone", "access": "rw"},
        ],
        "primary_path": str(rw),
    }
    app = FastAPI()
    app.include_router(code_panel.router)
    try:
        yield TestClient(app), ws, rw, bulk
    finally:
        _SESSIONS.pop(SID, None)


def _seed(rw: Path, ws: Path) -> None:
    """The standard tree: matches, non-matches, heavy dirs, a binary, a GBK file."""
    _write(rw / "src" / "fuzzy.py", f"line one\nline two\n# {TOKEN}\n")
    _write(rw / "src" / "other.py", "print('nothing to see')\n")
    _write(rw / "docs" / "notes.md", f"# Notes\n{TOKEN} appears here\n")
    # Rank 1 for "fuzzy": the query spans a directory, not the basename.
    _write(rw / "fuzzy" / "inner.py", "x = 1\n")
    # Rank 2 for "fuzzy": a pure subsequence of the path (f-u-z-z-y).
    _write(rw / "f" / "u" / "z" / "z" / "y.txt", "deep\n")
    # Heavy dirs — CONTENT search must never look inside these.
    _write(rw / "node_modules" / "pkg" / "index.js", f"// {TOKEN}\n")
    _write(rw / ".git" / "hooks" / "pre-commit", f"{TOKEN}\n")
    _write(rw / ".next" / "bundle.js", f"{TOKEN}\n")
    # Binary — a NUL byte means "not text", so it is skipped even though the
    # token is present as bytes.
    (rw / "bin.dat").write_bytes(b"\x00\x01" + TOKEN.encode() + b"\x00")
    # Non-utf-8 — must still match, because the decoder probes gbk.
    (rw / "gbk.txt").write_bytes(GBK_WORD.encode("gbk") + b"\n")
    # A file in the OTHER root, to prove search is scoped to the asked root.
    _write(ws / "session_note.txt", f"{TOKEN} in the workspace\n")


def _search(c: TestClient, **kw) -> dict:
    params = {**Q, "root": "f_rw", "mode": "name", "q": "", **kw}
    return c.get("/api/code/search", params=params).json()


# --------------------------------------------------------------------------- #
# Name search (⌘P)
# --------------------------------------------------------------------------- #
def test_name_search_matches_and_nothing_else(panel):
    """Only files whose path matches come back; the non-match stays out."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, q="other")

    assert body["ok"] is True and body["mode"] == "name"
    assert [h["path"] for h in body["hits"]] == ["src/other.py"]
    assert body["truncated"] is False
    assert body["scanned"] > 0
    assert isinstance(body["elapsed_ms"], int) and body["elapsed_ms"] >= 0


def test_name_search_ranks_basename_hits_first(panel):
    """Ordering is pinned: basename substring → path substring → subsequence.

    The three files are chosen so each lands in a different rank, which proves
    the ranking (a plain alphabetical sort would return them in the opposite
    order for two of the three). The tie-breakers after rank are depth, length,
    then name, so results are deterministic across filesystems.
    """
    c, _ws, _rw, _bulk = panel

    body = _search(c, q="fuzzy")

    assert [h["path"] for h in body["hits"]] == [
        "src/fuzzy.py",  # rank 0 — "fuzzy" in the basename
        "fuzzy/inner.py",  # rank 1 — "fuzzy" spans a directory
        "f/u/z/z/y.txt",  # rank 2 — subsequence only
    ]
    # A file with no 'f' at all must not sneak in via fuzzy matching.
    assert "src/other.py" not in [h["path"] for h in body["hits"]]


def test_name_search_returns_files_not_directories(panel):
    """A Hit is just a path and ⌘P opens a file, so ``src`` itself is not a hit."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, q="src")

    assert [h["path"] for h in body["hits"]] == ["src/fuzzy.py", "src/other.py"]
    assert "src" not in [h["path"] for h in body["hits"]]


def test_name_search_skips_heavy_dirs(panel):
    """The only file named ``index.js`` lives in ``node_modules`` → no hits."""
    c, _ws, rw, _bulk = panel
    # Sanity: the file really is there, so the miss below is the prune's doing.
    assert (rw / "node_modules" / "pkg" / "index.js").is_file()

    body = _search(c, q="index.js")

    assert body["hits"] == []
    assert body["ok"] is True


def test_name_search_is_root_relative_and_posix(panel):
    """Paths are root-relative with ``/`` separators (no absolute leak)."""
    c, _ws, _rw, _bulk = panel

    hits = _search(c, q="notes")["hits"]

    assert [h["path"] for h in hits] == ["docs/notes.md"]
    assert not hits[0]["path"].startswith("/")


# --------------------------------------------------------------------------- #
# Content search (⇧⌘F) — the heavy-dir regression lives here
# --------------------------------------------------------------------------- #
def test_content_search_finds_real_matches_with_line_numbers(panel):
    c, _ws, _rw, _bulk = panel

    body = _search(c, mode="content", q=TOKEN)

    assert body["ok"] is True and body["mode"] == "content"
    by_path = {h["path"]: h for h in body["hits"]}
    assert set(by_path) == {"src/fuzzy.py", "docs/notes.md"}
    # Line numbers are 1-based and point at the matching line.
    assert by_path["src/fuzzy.py"]["line"] == 3
    assert TOKEN in by_path["src/fuzzy.py"]["text"]
    assert by_path["docs/notes.md"]["line"] == 2
    assert body["truncated"] is False
    assert body["scanned"] >= 2


def test_content_search_never_descends_into_heavy_dirs(panel):
    """THE regression: a guaranteed match inside ``node_modules``/.git/.next.

    ``grep_files``' only prune is ``_path_denied``, which does not exclude
    ``node_modules`` — so a direct port would walk it, spend the budget, and
    return these hits. The planted files are read back first, so their absence
    from the hits can only mean the skip set pruned them.
    """
    c, _ws, rw, _bulk = panel
    # The token is present in all three heavy dirs — read them to prove it.
    assert TOKEN in (rw / "node_modules" / "pkg" / "index.js").read_text()
    assert TOKEN in (rw / ".git" / "hooks" / "pre-commit").read_text()
    assert TOKEN in (rw / ".next" / "bundle.js").read_text()

    paths_hit = [h["path"] for h in _search(c, mode="content", q=TOKEN)["hits"]]

    # Order is os.walk order (not a contract); membership is what matters.
    assert sorted(paths_hit) == ["docs/notes.md", "src/fuzzy.py"]
    for h in paths_hit:
        assert not h.startswith(("node_modules", ".git", ".next"))


def test_content_search_skips_binary_files(panel):
    """A NUL byte marks the file as binary; the token as bytes must not surface."""
    c, _ws, rw, _bulk = panel
    assert TOKEN.encode() in (rw / "bin.dat").read_bytes()

    paths_hit = [h["path"] for h in _search(c, mode="content", q=TOKEN)["hits"]]

    assert "bin.dat" not in paths_hit


def test_content_search_reads_gbk_files(panel):
    """The panel's encoding probe reaches search too, unlike grep_files' utf-8.

    Asserted on the file's bytes: the query word has a GBK-only byte sequence,
    so an utf-8-only read (``errors="ignore"``) would drop it and return nothing.
    """
    c, _ws, rw, _bulk = panel
    raw = (rw / "gbk.txt").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # not utf-8 → the probe has to do the work

    hits = _search(c, mode="content", q=GBK_WORD)["hits"]

    assert [h["path"] for h in hits] == ["gbk.txt"]
    assert hits[0]["text"] == GBK_WORD


def test_content_search_can_be_scoped_by_regex_not_substring(panel):
    """``q`` is a REGEX, like ``grep_files``: anchors and classes must work."""
    c, _ws, _rw, _bulk = panel

    hits = _search(c, mode="content", q=r"^line (one|two)$")["hits"]

    assert [h["path"] for h in hits] == ["src/fuzzy.py", "src/fuzzy.py"]
    assert [h["line"] for h in hits] == [1, 2]


# --------------------------------------------------------------------------- #
# Explicit truncation (brief §1-2)
# --------------------------------------------------------------------------- #
def test_content_search_truncates_at_the_hit_budget(panel):
    """600 matching lines against a 500-hit budget → truncated, not silent.

    ``limit`` defaults to 50, so the RESPONSE is paged to 50 while the SCAN ran
    to its 500-hit cap; ``truncated`` must be true either way (more matches
    exist than are returned).
    """
    c, _ws, rw, _bulk = panel
    _write(rw / "many.txt", "".join(f"BULK_HIT line {i}\n" for i in range(600)))

    body = _search(c, mode="content", q="BULK_HIT")

    assert body["truncated"] is True
    assert len(body["hits"]) == code_panel.SEARCH_LIMIT_DEFAULT
    assert body["scanned"] >= 1  # at least the bulk file was read
    assert all(h["path"] == "many.txt" for h in body["hits"])


def test_content_search_truncates_when_the_page_is_smaller_than_the_matches(panel):
    """Fewer results returned than matched is also a truncation."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, mode="content", q=TOKEN, limit=1)

    assert body["ok"] is True
    assert len(body["hits"]) == 1
    assert body["truncated"] is True


def test_content_limit_is_clamped_to_the_maximum(panel):
    """A limit over the cap is clamped (200), never rejected or honoured."""
    c, _ws, rw, _bulk = panel
    _write(rw / "many.txt", "".join(f"BULK_HIT line {i}\n" for i in range(600)))

    body = _search(c, mode="content", q="BULK_HIT", limit=9999)

    assert body["ok"] is True
    assert len(body["hits"]) == code_panel.SEARCH_LIMIT_MAX


def test_name_search_truncates_at_the_entry_budget(panel, monkeypatch):
    """The 20000-entry walk budget, exercised over a real 20-file directory.

    A real over-budget tree would need 20001 entries, which is minutes of
    filesystem work for a path that is identical to this one; the budget is the
    only thing shrunk. A separate root keeps the ordering deterministic: the
    walk visits ``f00..f19`` in order and stops one entry PAST the budget, so
    ``scanned`` is exactly ``budget + 1`` and the first ``budget`` files are the
    hits.
    """
    c, _ws, _rw, bulk = panel
    for i in range(20):
        _write(bulk / f"f{i:02d}.txt", "x\n")
    monkeypatch.setattr(code_panel, "SEARCH_NAME_MAX_ENTRIES", 5)

    body = _search(c, root="f_bulk", q="f")

    assert body["ok"] is True
    assert body["truncated"] is True
    assert body["scanned"] == 6
    assert [h["path"] for h in body["hits"]] == [f"f{i:02d}.txt" for i in range(5)]


def test_a_small_tree_is_not_truncated(panel):
    """Control for the test above: the same search under the real budget is complete."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, q="fuzzy")

    assert body["truncated"] is False


# --------------------------------------------------------------------------- #
# Fence + bad input (never a 500)
# --------------------------------------------------------------------------- #
def test_unknown_root_uses_the_house_error_code(panel):
    c, _ws, _rw, _bulk = panel

    body = _search(c, root="f_ghost", q="x")

    assert body["ok"] is False
    assert body["code"] == "unknown-root"


def test_a_missing_root_directory_reports_root_missing(panel):
    """A mount whose directory was removed is ``root-missing``, not ``unknown-root``."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, root="f_gone", q="x")

    assert body["ok"] is False
    assert body["code"] == "root-missing"


def test_an_unknown_session_is_refused(panel):
    c, _ws, _rw, _bulk = panel

    body = c.get("/api/code/search", params={**Q, "session_id": "ghost", "q": "x"}).json()

    assert body["ok"] is False
    assert body["code"] == "unknown-root"


def test_search_works_on_the_session_workspace_root(panel):
    """The session workspace is a root like any mount."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, root="session", mode="content", q=TOKEN)

    assert body["ok"] is True
    assert [h["path"] for h in body["hits"]] == ["session_note.txt"]


def test_invalid_regex_fails_clearly_instead_of_500(panel):
    c, _ws, _rw, _bulk = panel

    r = c.get("/api/code/search", params={**Q, "root": "f_rw", "mode": "content", "q": "("})

    assert r.status_code == 200  # house style: 200 + ok:false, never a 500
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "invalid-regex"
    assert body["message"]


@pytest.mark.parametrize("q", ["", "   "])
def test_an_empty_query_is_refused_not_a_500(panel, q):
    c, _ws, _rw, _bulk = panel

    body = _search(c, q=q)

    assert body["ok"] is False
    assert body["code"] == "invalid-query"


def test_an_oversized_query_is_refused_not_a_500(panel):
    c, _ws, _rw, _bulk = panel

    body = _search(c, mode="content", q="a" * (code_panel.SEARCH_MAX_QUERY_BYTES + 1))

    assert body["ok"] is False
    assert body["code"] == "invalid-query"


def test_an_unknown_mode_is_refused_not_a_500(panel):
    c, _ws, _rw, _bulk = panel

    body = _search(c, mode="symbols", q="x")

    assert body["ok"] is False
    assert body["code"] == "invalid-mode"


def test_the_fence_runs_before_the_query_is_validated(panel):
    """An unreachable root wins over a bad query — the fence is the first gate,
    matching ``list``/``read`` (design §4.6 gate order)."""
    c, _ws, _rw, _bulk = panel

    body = _search(c, root="f_ghost", mode="content", q="(")

    assert body["ok"] is False
    assert body["code"] == "unknown-root"