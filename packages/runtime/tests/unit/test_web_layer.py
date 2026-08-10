"""Unit tests for the built-in web layer (citations-design.md §4)."""

from __future__ import annotations

import json

import pytest

from ginno_runtime.knowledge import citations as cit
from ginno_runtime.knowledge import web_usage
from ginno_runtime.web import engines, fetch
from ginno_runtime.web.config import WebConfig, load_web_config

pytestmark = pytest.mark.unit


# ------------------------------ engines ------------------------------ #
def test_ddg_unwrap():
    assert engines._ddg_unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx&foo=1") == "https://a.com/x"
    assert engines._ddg_unwrap("https://example.com/page") == "https://example.com/page"
    assert engines._ddg_unwrap("javascript:alert(1)") == ""


def test_ddg_parse(monkeypatch):
    html = """
    <div class="result">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fpage">
        Docs <b>Example</b></a>
      <a class="result__snippet">A <b>snippet</b> here.</a>
    </div>
    <div class="result">
      <a rel="nofollow" class="result__a" href="https://plain.example.org/2">Plain title</a>
      <a class="result__snippet">Second.</a>
    </div>
    """
    monkeypatch.setattr(engines, "_http_get", lambda url, t, headers=None: html.encode())
    hits = engines._ddg("q", {}, 5)
    assert len(hits) == 2
    assert hits[0].url == "https://docs.example.com/page"
    assert hits[0].title == "Docs Example"
    assert hits[0].snippet == "A snippet here."
    assert hits[1].url == "https://plain.example.org/2"


def test_searxng_requires_base_url():
    with pytest.raises(engines.EngineError):
        engines._searxng("q", {}, 5)


def test_searxng_parse(monkeypatch):
    payload = json.dumps(
        {"results": [{"title": "T", "url": "https://x.io/a", "content": "C"}, {"title": "no url"}]}
    ).encode()
    monkeypatch.setattr(engines, "_http_get", lambda url, t, headers=None: payload)
    hits = engines._searxng("q", {"base_url": "http://127.0.0.1:8888/"}, 5)
    assert hits == [engines.SearchHit(title="T", url="https://x.io/a", snippet="C")]


def test_search_unknown_engine():
    with pytest.raises(engines.EngineError):
        engines.search("q", "nope", {}, 5, 5)
    assert "duckduckgo" in engines.ENGINES


def test_search_caps_results(monkeypatch):
    hits = [engines.SearchHit("t", f"https://e.com/{i}", "") for i in range(12)]
    monkeypatch.setitem(engines.ENGINES, "duckduckgo", lambda q, cfg, t: hits)
    assert len(engines.search("q", "duckduckgo", {}, 5, 3)) == 3


# ------------------------------- fetch ------------------------------- #
@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://x/", "http://127.0.0.1/", "http://localhost/x", "http://10.0.0.5/"],
)
def test_fetch_guards(url):
    with pytest.raises(fetch.FetchError):
        fetch.fetch_page(url, timeout_s=1)


def test_fetch_extracts_readable_text(monkeypatch):
    html = (
        "<html><head><title> 页面标题 </title><style>x{}</style></head>"
        "<body><nav>menu</nav><h1>大题</h1><p>第一段 <b>重点</b></p>"
        "<script>evil()</script><p>第二段</p></body></html>"
    ).encode()
    monkeypatch.setattr(
        fetch, "_get_pinned", lambda url, t: ("https://e.com/p", "text/html", html)
    )
    page = fetch.fetch_page("https://e.com/p")
    assert page["title"] == "页面标题"
    assert "大题" in page["text"] and "第一段" in page["text"] and "第二段" in page["text"]
    assert "evil()" not in page["text"] and "menu" not in page["text"]
    assert page["truncated"] is False


def test_resolve_public_rejects_mixed_answers(monkeypatch):
    """A DNS answer mixing public + private IPs is refused outright (the
    connection would pin ONE resolved address — a mixed answer is untrusted)."""

    def fake_gai(host, port, proto=0):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("169.254.169.254", port)),  # cloud metadata
        ]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake_gai)
    with pytest.raises(fetch.FetchError):
        fetch._resolve_public("rebind.example", 443)


def test_resolve_public_accepts_all_public(monkeypatch):
    monkeypatch.setattr(
        fetch.socket, "getaddrinfo", lambda h, p, proto=0: [(2, 1, 6, "", ("93.184.216.34", p))]
    )
    infos = fetch._resolve_public("example.com", 443)
    assert infos and infos[0][4][0] == "93.184.216.34"


# ----------------------------- web tools ----------------------------- #
def test_build_web_tools_disabled(isolated_home):
    (isolated_home / "settings.json").write_text(json.dumps({"web": {"enabled": False}}))
    from ginno_runtime.tools.web_tools import build_web_tools

    assert build_web_tools("s") == []


def test_web_search_registers_sources(isolated_home, monkeypatch):
    from ginno_runtime.tools import web_tools

    monkeypatch.setattr(
        web_tools,
        "engine_search",
        lambda q, name, cfg, t, n: [
            engines.SearchHit("Docs", "https://docs.example.com/page", "snip-A"),
            engines.SearchHit("Blog", "https://blog.example.com/x", "snip-B"),
        ],
    )
    tools = web_tools.build_web_tools("sess-w")
    assert {t.name for t in tools} == {"web_search", "web_fetch"}
    cit.begin_turn_sources("sess-w")
    ws = next(t for t in tools if t.name == "web_search")
    out = ws.invoke({"query": "langgraph checkpoint"})
    assert "[s1] Docs" in out and "[s2] Blog" in out
    assert "snip-A" in out
    srcs = cit.peek_turn_sources("sess-w")
    assert [s["id"] for s in srcs] == ["s1", "s2"]
    assert srcs[0]["engine"] == "duckduckgo" and srcs[0]["depth"] == "snippet"
    # engine telemetry recorded
    data = json.loads(web_usage.web_usage_path().read_text())
    assert data["engines"]["duckduckgo"]["searches"] == 1


def test_web_search_error_contract(isolated_home, monkeypatch):
    from ginno_runtime.tools import web_tools

    def boom(*a, **k):
        raise engines.EngineError("引擎挂了")

    monkeypatch.setattr(web_tools, "engine_search", boom)
    tools = web_tools.build_web_tools("s2")
    ws = next(t for t in tools if t.name == "web_search")
    out = ws.invoke({"query": "x"})
    assert out.startswith("[error]")


def test_web_fetch_upgrades_source(isolated_home, monkeypatch):
    from ginno_runtime.tools import web_tools

    monkeypatch.setattr(
        web_tools, "fetch_page",
        lambda url, timeout_s=15: {"url": url, "final_url": url, "title": "全文", "text": "正文…", "truncated": False},
    )
    tools = web_tools.build_web_tools("sess-f")
    cit.begin_turn_sources("sess-f")
    cit.register_source_for("sess-f", {"kind": "web", "identity": "https://e.com/p", "title": "t",
                                       "origin": "search", "depth": "snippet", "engine": "duckduckgo"})
    wf = next(t for t in tools if t.name == "web_fetch")
    out = wf.invoke({"url": "https://e.com/p"})
    assert "已读取原文" in out and "[s1]" in out
    src = cit.peek_turn_sources("sess-f")[0]
    assert src["depth"] == "fetched" and src["title"] == "全文"
    data = json.loads(web_usage.web_usage_path().read_text())
    assert data["domains"]["e.com"]["fetched"] == 1


def test_web_fetch_private_rejected(isolated_home):
    from ginno_runtime.tools import web_tools

    tools = web_tools.build_web_tools("s3")
    wf = next(t for t in tools if t.name == "web_fetch")
    out = wf.invoke({"url": "http://192.168.1.10/"})
    assert out.startswith("[error]")


# ----------------------------- web usage ----------------------------- #
def test_web_usage_summary(isolated_home):
    web_usage.record_search("duckduckgo", 5)
    web_usage.record_search("duckduckgo", 3)
    web_usage.record_cited("https://docs.example.com/a", engine="duckduckgo", fetched=True)
    web_usage.record_fetched("https://www.docs.example.com/b")
    s = web_usage.summary()
    eng = s["engines"][0]
    assert eng["engine"] == "duckduckgo" and eng["searches"] == 2
    assert eng["hits_cited"] == 1 and eng["cite_rate"] == 0.5
    doms = {d["domain"]: d for d in s["top_domains"]}
    assert doms["docs.example.com"]["cited"] == 1
    assert doms["docs.example.com"]["fetched"] == 2  # cited(fetched) + fetched


def test_web_config_defaults_and_load(isolated_home):
    cfg = load_web_config({})
    assert cfg.enabled is True and cfg.default_engine == "duckduckgo"
    assert isinstance(cfg, WebConfig)
    cfg2 = load_web_config({"web": {"default_engine": "searxng", "engines": {"searxng": {"base_url": "http://x"}}}})
    assert cfg2.default_engine == "searxng"
    assert cfg2.engine_cfg("searxng")["base_url"] == "http://x"


def test_search_records_zero_hit_searches(isolated_home, monkeypatch):
    """Zero-hit searches still enter the searches denominator (cite_rate)."""
    from ginno_runtime.tools import web_tools

    monkeypatch.setattr(web_tools, "engine_search", lambda *a, **k: [])
    tools = web_tools.build_web_tools("s-zero")
    ws = next(t for t in tools if t.name == "web_search")
    out = ws.invoke({"query": "nothing"})
    assert "没有找到" in out
    data = json.loads(web_usage.web_usage_path().read_text())
    assert data["engines"]["duckduckgo"]["searches"] == 1
    assert data["engines"]["duckduckgo"]["results"] == 0


def test_record_cited_does_not_bump_fetched(isolated_home):
    """record_cited must not increment the fetched counter (record_fetched at
    fetch time is the single counting point — no double count)."""
    web_usage.record_cited("https://a.example/x", engine="duckduckgo")
    data = json.loads(web_usage.web_usage_path().read_text())
    d = data["domains"]["a.example"]
    assert d["cited"] == 1
    assert d.get("fetched", 0) == 0


def test_ensure_web_permissions_migration(isolated_home):
    import json as _json

    from ginno_runtime.permission.policy import ensure_web_permissions

    sp = isolated_home / "settings.json"
    # existing perms block without the web tools -> migrated into allow
    sp.write_text(_json.dumps({"permissions": {"allow": ["read_file"], "deny": ["Bash(rm -rf *)"], "ask": ["Bash(*)"]}}))
    ensure_web_permissions()
    perms = _json.loads(sp.read_text())["permissions"]
    assert "web_search" in perms["allow"] and "web_fetch" in perms["allow"]
    # idempotent
    ensure_web_permissions()
    perms = _json.loads(sp.read_text())["permissions"]
    assert perms["allow"].count("web_search") == 1
    # a deliberate user deny is respected (no allow added for it)
    sp.write_text(_json.dumps({"permissions": {"allow": [], "deny": ["web_search"], "ask": []}}))
    ensure_web_permissions()
    perms = _json.loads(sp.read_text())["permissions"]
    assert "web_search" not in perms["allow"]
    assert "web_fetch" in perms["allow"]
    # no permissions block -> untouched
    sp.write_text(_json.dumps({"theme": "dark"}))
    ensure_web_permissions()
    assert "permissions" not in _json.loads(sp.read_text())
