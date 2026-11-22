from ginno_runtime.agents.registry import (
    _DEV_PROMPT,
    _LEGACY_DEV_PROMPT,
    ensure_dev_discipline,
)

_LEGACY_DEV_PROMPT_TEXT = (
    "You are Dev Agent, a software-engineering assistant. You help with "
    "code, PRs, debugging, and repo operations. Prefer concrete actions "
    "using your tools. Be concise."
)


def test_legacy_prompt_matches_seed_verbatim():
    # 守卫：ensure_dev_discipline 的「逐字旧种子才换」契约依赖此串精确——
    # 若 _LEGACY_DEV_PROMPT 被改写，升级会静默跳过所有旧装用户。
    assert _LEGACY_DEV_PROMPT == _LEGACY_DEV_PROMPT_TEXT


def test_ensure_dev_discipline_upgrades_legacy_seed(tmp_path, monkeypatch):
    from ginno_runtime import paths

    monkeypatch.setattr(paths, "agents_dir", lambda: tmp_path)
    import json

    # 模拟旧装：dev.json 落着逐字旧种子
    (tmp_path / "dev.json").write_text(
        json.dumps(
            {
                "id": "dev",
                "name": "Dev Agent",
                "icon": "terminal",
                "color": "blue",
                "system_prompt": _LEGACY_DEV_PROMPT_TEXT,
                "provider": "custom",
                "tools_allow": ["*"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ensure_dev_discipline()
    cfg = json.loads((tmp_path / "dev.json").read_text(encoding="utf-8"))
    assert cfg["system_prompt"] == _DEV_PROMPT
    assert cfg["system_prompt"] != _LEGACY_DEV_PROMPT_TEXT


def test_ensure_dev_discipline_never_touches_customized(tmp_path, monkeypatch):
    from ginno_runtime import paths

    monkeypatch.setattr(paths, "agents_dir", lambda: tmp_path)
    import json

    custom = "我的定制 prompt，绝不覆盖"
    (tmp_path / "dev.json").write_text(
        json.dumps(
            {
                "id": "dev",
                "name": "Dev Agent",
                "system_prompt": custom,
                "provider": "custom",
                "tools_allow": ["*"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ensure_dev_discipline()
    cfg = json.loads((tmp_path / "dev.json").read_text(encoding="utf-8"))
    assert cfg["system_prompt"] == custom


def test_ensure_dev_discipline_idempotent(tmp_path, monkeypatch):
    from ginno_runtime import paths

    monkeypatch.setattr(paths, "agents_dir", lambda: tmp_path)
    import json

    (tmp_path / "dev.json").write_text(
        json.dumps(
            {
                "id": "dev",
                "name": "Dev Agent",
                "system_prompt": _LEGACY_DEV_PROMPT_TEXT,
                "provider": "custom",
                "tools_allow": ["*"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ensure_dev_discipline()
    first = (tmp_path / "dev.json").read_text(encoding="utf-8")
    ensure_dev_discipline()  # 二次运行：已是新版 → no-op，内容不变
    assert (tmp_path / "dev.json").read_text(encoding="utf-8") == first


def test_new_prompt_carries_flow_conventions():
    # 钉住新 prompt 承载的研发流程关键词（防未来误删关键纪律）
    p = _DEV_PROMPT
    for kw in (
        "worktree",
        "ruff",
        "tsc",
        "vitest",
        "cargo fmt",
        "现象",
        "根因",
        "修复",
        "验证",
        "Simplified Chinese",
        "regression test",
    ):
        assert kw in p, kw
