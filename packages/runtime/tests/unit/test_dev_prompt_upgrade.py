from ginno_runtime.agents.registry import _DEV_PROMPT


def test_seed_carries_new_dev_prompt(tmp_path, monkeypatch):
    from ginno_runtime import paths

    monkeypatch.setattr(paths, "agents_dir", lambda: tmp_path)
    from ginno_runtime.agents import registry as R

    R.ensure_seeded()
    import json

    cfg = json.loads((tmp_path / "dev.json").read_text(encoding="utf-8"))
    assert cfg["system_prompt"] == _DEV_PROMPT


def test_dev_prompt_carries_flow_conventions():
    # 钉住研发流程关键词（防未来误删关键纪律）
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
        assert kw in _DEV_PROMPT, kw
