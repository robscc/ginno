"""wecom last_chat_key 持久化（2026-11-23 会话 4cacd4a4 卡漏发实案）。

sidecar 重启（含 dev-swap）后纯内存态 last_chat_key 归零 → 重启后到用户
下次企微说话之间的桌面 ask_user 企微镜像静默跳过。修复：入站刷新时原子
落盘，adapter 启动即恢复。
"""

from ginno_runtime.channels.wecom import adapter as A


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(A.paths, "home", lambda: tmp_path)
    A._save_last_chat_key("SongChenChuan|single")
    assert A._load_last_chat_key() == "SongChenChuan|single"


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(A.paths, "home", lambda: tmp_path)
    assert A._load_last_chat_key() is None


def test_load_corrupt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(A.paths, "home", lambda: tmp_path)
    d = tmp_path / "channels"
    d.mkdir(parents=True)
    (d / "wecom_last_chat_key").write_text("   \n", encoding="utf-8")
    assert A._load_last_chat_key() is None


def test_adapter_init_restores_persisted_key(tmp_path, monkeypatch):
    monkeypatch.setattr(A.paths, "home", lambda: tmp_path)
    A._save_last_chat_key("u1|single")
    ad = A.WeComChannelAdapter()
    assert ad.last_chat_key == "u1|single"


def test_save_failure_is_silent(tmp_path, monkeypatch):
    # 落盘失败绝不炸（镜像触达尽力而为）：home 指向只读路径
    import pathlib

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.setattr(A.paths, "home", lambda: ro)
    try:
        A._save_last_chat_key("x")  # 不应抛
    finally:
        ro.chmod(0o700)
    assert A._load_last_chat_key() is None
