"""settings_store：损坏拒写 / 原子写 / 权限 / 并发 / 备份 的行为契约。

settings-integrity（2026-11-22）验收面：
- 三态区分（missing / ok / corrupt）——「不存在」与「损坏」必须可区分；
- 损坏时任何写入口（atomic_write / update）拒写且**逐字节保留原文件**；
- 原子写：同目录 tmp + os.replace；文件权限 0600；不留 tmp 残骸；
- 写失败（replace 抛错）原文件不动；
- 并发 update 无丢失更新；
- backup 非破坏（原文件不动、不覆盖已有备份）。
"""

from __future__ import annotations

import json as stdjson
import os
import threading

import pytest

from ginno_runtime import paths, settings_store


def _write_raw(text: str) -> None:
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _raw_bytes() -> bytes:
    return paths.settings_path().read_bytes()


CORRUPT_SAMPLE = '{"providers": {"custom": {"api_key": "sk-live-1234"'  # 截断


# ---- 三态 -------------------------------------------------------------------


def test_missing_vs_corrupt_distinguished(tmp_path):
    doc = settings_store.load_doc()
    assert doc.status == settings_store.STATUS_MISSING
    assert doc.data == {}

    _write_raw('{"language": "zh-CN"}')
    doc = settings_store.load_doc()
    assert doc.status == settings_store.STATUS_OK
    assert doc.data == {"language": "zh-CN"}

    _write_raw(CORRUPT_SAMPLE)
    doc = settings_store.load_doc()
    assert doc.status == settings_store.STATUS_CORRUPT


def test_corrupt_status_and_error(tmp_path):
    _write_raw(CORRUPT_SAMPLE)
    doc = settings_store.load_doc()
    assert doc.corrupt
    assert doc.error  # 解析错误可展示（供 UI 横幅）


def test_top_level_non_object_is_corrupt(tmp_path):
    _write_raw('["not", "an", "object"]')
    doc = settings_store.load_doc()
    assert doc.corrupt


def test_empty_file_treated_as_missing(tmp_path):
    """零字节 = 无信息可保留，与不存在同语义（ensure_layout 同判断）。"""
    _write_raw("")
    doc = settings_store.load_doc()
    assert doc.status == settings_store.STATUS_MISSING
    # 可写默认
    settings_store.atomic_write({"a": 1})
    assert settings_store.load_doc().data == {"a": 1}


def test_whitespace_only_file_treated_as_missing(tmp_path):
    _write_raw("   \n  ")
    assert settings_store.load_doc().status == settings_store.STATUS_MISSING


# ---- 损坏拒写 ---------------------------------------------------------------


def test_atomic_write_refuses_when_corrupt_preserves_bytes(tmp_path):
    _write_raw(CORRUPT_SAMPLE)
    before = _raw_bytes()
    with pytest.raises(settings_store.SettingsCorruptError):
        settings_store.atomic_write({"evil": "overwrite"})
    assert _raw_bytes() == before


def test_update_refuses_when_corrupt_preserves_bytes(tmp_path):
    _write_raw(CORRUPT_SAMPLE)
    before = _raw_bytes()
    with pytest.raises(settings_store.SettingsCorruptError):

        def _mut(doc):
            doc["x"] = 1
            return doc

        settings_store.update(_mut)
    assert _raw_bytes() == before


def test_migrator_exception_propagates_without_write(tmp_path):
    """mutator 抛业务异常（如 env CAS 冲突）→ 不落盘、异常透传。"""
    settings_store.atomic_write({"keep": True})

    def _mut(doc):
        raise RuntimeError("conflict")

    with pytest.raises(RuntimeError):
        settings_store.update(_mut)
    assert stdjson.loads(_raw_bytes()) == {"keep": True}


# ---- 原子写 / 权限 / 失败路径 ------------------------------------------------


def test_atomic_write_same_dir_no_tmp_leftover(tmp_path):
    settings_store.atomic_write({"language": "en", "env": {"A": "b"}})
    p = paths.settings_path()
    assert p.exists()
    leftovers = [q.name for q in p.parent.iterdir() if q.name.startswith(".settings")]
    assert leftovers == []


def test_atomic_write_file_mode_0600(tmp_path):
    settings_store.atomic_write({"providers": {"api_key": "sk-x"}})
    mode = paths.settings_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_write_failure_leaves_original(tmp_path, monkeypatch):
    settings_store.atomic_write({"original": True})
    before = _raw_bytes()

    real_replace = os.replace

    def _boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(settings_store.SettingsWriteError):
        settings_store.atomic_write({"original": False})
    monkeypatch.setattr(os, "replace", real_replace)
    assert _raw_bytes() == before
    # tmp 清理
    leftovers = [q.name for q in paths.settings_path().parent.iterdir()
                 if q.name.startswith(".settings")]
    assert leftovers == []


def test_update_merges_without_losing_sibling_keys(tmp_path):
    settings_store.atomic_write({"providers": {"custom": {"api_key": "k"}}, "language": "en"})

    def _mut(doc):
        doc["language"] = "zh-CN"
        return doc

    settings_store.update(_mut)
    doc = settings_store.load_doc()
    assert doc.data["language"] == "zh-CN"
    assert doc.data["providers"]["custom"]["api_key"] == "k"


def test_update_mutator_returning_none_is_noop(tmp_path):
    settings_store.atomic_write({"a": 1})
    mtime = paths.settings_path().stat().st_mtime_ns
    settings_store.update(lambda doc: None)
    assert paths.settings_path().stat().st_mtime_ns == mtime


# ---- 并发 -------------------------------------------------------------------


def test_concurrent_updates_no_lost_update(tmp_path):
    """N 线程各 update 一个不同键 → 全部在盘（单写锁保证一致性）。"""
    settings_store.atomic_write({})
    n = 24
    threads = []
    for i in range(n):

        def _mut(doc, i=i):
            doc[f"key_{i}"] = i
            return doc

        t = threading.Thread(target=settings_store.update, args=(_mut,))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = settings_store.read_settings()
    assert {data[f"key_{i}"] for i in range(n)} == set(range(n))


# ---- 备份（非破坏）---------------------------------------------------------


def test_backup_copies_bytes_and_leaves_original(tmp_path):
    _write_raw(CORRUPT_SAMPLE)
    before = _raw_bytes()
    target = settings_store.backup(tag="corrupt")
    assert target.exists()
    assert target.read_bytes() == before
    assert target.name.startswith("settings.json.bak-")
    assert _raw_bytes() == before  # 原文件逐字节不动
    assert (target.stat().st_mode & 0o777) == 0o600


def test_backup_never_overwrites_existing(tmp_path):
    _write_raw("{}")
    t1 = settings_store.backup()
    paths.settings_path().write_text('{"changed": true}')
    t2 = settings_store.backup()
    assert t1 != t2
    assert stdjson.loads(t1.read_text()) == {}


def test_backup_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        settings_store.backup()


# ---- backup/repair 硬线（settings-integrity 复核补强）-----------------------


class _FrozenDatetime:
    """duck-type ``datetime``：now() 恒定 → backup 连续同名，逼出排他路径。"""

    @staticmethod
    def now(tz=None):
        from datetime import datetime as _dt

        return _dt(2026, 11, 22, 12, 0, 0, tzinfo=tz)


def test_backup_exclusive_creation_same_timestamp(tmp_path, monkeypatch):
    """同一时间戳下多次备份：O_EXCL 排他，撞名加序号，永不互相覆盖。"""
    monkeypatch.setattr(settings_store, "datetime", _FrozenDatetime)
    _write_raw('{"v": 1}')
    t1 = settings_store.backup(tag="x")
    paths.settings_path().write_text('{"v": 2}')
    t2 = settings_store.backup(tag="x")
    paths.settings_path().write_text('{"v": 3}')
    t3 = settings_store.backup(tag="x")
    assert len({t1, t2, t3}) == 3
    assert t1.name.endswith(".bak-20261122T120000Z-x")
    assert t2.name == t1.name + ".1"
    assert t3.name == t1.name + ".2"
    # 各自内容对应备份时刻的原字节（后写者不截断前者）
    assert stdjson.loads(t1.read_text()) == {"v": 1}
    assert stdjson.loads(t2.read_text()) == {"v": 2}
    assert stdjson.loads(t3.read_text()) == {"v": 3}


def test_backup_mode_0600_even_with_permissive_umask(tmp_path, monkeypatch):
    """umask=0（最宽松）下备份文件仍恰 0600——权限来自创建参数
    （O_CREAT mode 0600），不依赖事后 chmod，不存在 0644 落地窗口。"""
    old = os.umask(0)
    try:
        _write_raw('{"a": 1}')
        target = settings_store.backup()
    finally:
        os.umask(old)
    assert (target.stat().st_mode & 0o777) == 0o600


def test_backup_write_failure_removes_partial(tmp_path, monkeypatch):
    """写中途失败：半成品备份被清理（不留下看似可用的不完整文件），
    原文件不动，异常可观察。"""
    _write_raw(CORRUPT_SAMPLE)
    before = _raw_bytes()

    class _ExplodingFile:
        def __init__(self, fd): self._fd = fd
        def write(self, b): raise OSError("disk full (simulated)")
        def flush(self): pass
        def fileno(self): return self._fd
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def _fdopen(fd, mode="w", **kw):
        return _ExplodingFile(fd)

    monkeypatch.setattr(settings_store.os, "fdopen", _fdopen)
    with pytest.raises(settings_store.SettingsWriteError):
        settings_store.backup()
    # 零残留：目标目录里没有任何 bak 文件
    baks = list(paths.settings_path().parent.glob("settings.json.bak*"))
    assert baks == []
    assert _raw_bytes() == before


def test_repair_corrupt_then_stale_repair_rejected(tmp_path):
    """损坏 → repair 落盘（带 pre-repair 备份）；文件健康后迟到的 repair
    被拒（SettingsNotCorruptError），健康内容不被旧快照覆盖。"""
    _write_raw(CORRUPT_SAMPLE)
    result = settings_store.repair({"language": "zh-CN"})
    assert result["backup"] is not None
    assert result["backup"].read_bytes() == CORRUPT_SAMPLE.encode()
    assert stdjson.loads(_raw_bytes().decode()) == {"language": "zh-CN"}

    with pytest.raises(settings_store.SettingsNotCorruptError):
        settings_store.repair({"stale": "old-banner-snapshot"})
    # 第二次（陈旧）repair 未生效
    assert stdjson.loads(_raw_bytes().decode()) == {"language": "zh-CN"}


def test_repair_missing_writes_without_backup(tmp_path):
    """missing 态：无文件可覆盖 → repair 等同首次写，无备份动作。"""
    assert not paths.settings_path().exists()
    result = settings_store.repair({"fresh": True})
    assert result["backup"] is None
    assert stdjson.loads(_raw_bytes().decode()) == {"fresh": True}


def test_repair_backup_inherits_hardened_backup(tmp_path, monkeypatch):
    """repair 的 pre-repair 自动备份走同一 hardened 路径（排他 + 0600）。"""
    monkeypatch.setattr(settings_store, "datetime", _FrozenDatetime)
    _write_raw(CORRUPT_SAMPLE)
    r1 = settings_store.repair({"v": 1})
    # 再造损坏 + 第二次 repair：同冻结时间戳 → 序号递增，不覆盖第一个
    paths.settings_path().write_text(CORRUPT_SAMPLE + "}}", encoding="utf-8")
    r2 = settings_store.repair({"v": 2})
    assert r1["backup"] != r2["backup"]
    for b in (r1["backup"], r2["backup"]):
        assert (b.stat().st_mode & 0o777) == 0o600


# ---- health -----------------------------------------------------------------


def test_health_reports_corrupt_with_stats(tmp_path):
    _write_raw(CORRUPT_SAMPLE)
    h = settings_store.health()
    assert h["status"] == "corrupt"
    assert h["error"]
    assert h["size"] == len(CORRUPT_SAMPLE.encode())
    assert "mtime" in h
