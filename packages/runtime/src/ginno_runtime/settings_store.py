"""settings.json 统一存储层：损坏拒写、同目录原子写、受限权限、单写锁。

背景（settings-integrity，2026-11-22）：settings.json 此前有 13+ 个分散的
直接 ``write_text`` 写入口（PUT /api/settings、providers、agents order、
permissions 迁移、mods、connectors、a2a hub、cli exec、ensure_layout 迁移），
全部是「读 → 内存改 → 整文件覆盖」。两个事故面：

1. **损坏放大**：任何一次解析失败都 fallback 成 ``{}`` 再写回——用户的
   原字节（可能只是末尾被截断）被一个近乎空白的文档覆盖，不可恢复。
2. **并发互踩**：两个设置页同时保存（PUT /settings 与 agents order、mods
   迁移）互相整文件覆盖，丢失更新。

本模块的契约：

- **三态区分**：``missing``（文件不存在——首次安装，可写默认）、``ok``
  （可解析的 JSON object）、``corrupt``（存在但解析失败/非 object——
  **任何写入口必须拒绝**，保留原字节等用户修复）。
- **原子写**：同目录隐藏 tmp 文件 + fsync + ``os.replace``；文件权限
  ``0600``（tmp 创建后立即 chmod，秘密不落在 0644 的窗口里）。
- **单写锁**：模块级 ``RLock`` 串行化进程内所有读-改-写；所有写入口
  （API、迁移、CAS）共用同一把锁，保证一致性。
- **非破坏恢复**：:func:`backup` 把原字节复制到旁路备份文件（原文件不动，
  排他创建、创建瞬间 0600、失败零残留），修复只能由用户显式完成（UI
  PUT 一份有效文档 / 手动编辑），绝不自动覆盖。:func:`repair` 是唯一
  损坏态写通道，且**仅限当前仍是损坏态**（ok 态拒绝——防迟到的旧横幅
  快照覆盖刚修好的设置）。

不在本层做的事：掩码往返（providers.py SEC-01 体系）、env CAS 语义
（env_store.py）——它们在本层之上组合。
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import paths
from . import jsonx as json

log = logging.getLogger(__name__)

# 进程内全局写锁：所有 settings.json 读-改-写共用（API 端点、迁移、CAS、
# ensure_layout）。RLock 允许同线程嵌套（update 内部再调 read/write）。
_LOCK = threading.RLock()

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_CORRUPT = "corrupt"

_FILE_MODE = 0o600


class SettingsCorruptError(RuntimeError):
    """settings.json 处于损坏态，写请求被拒绝（原字节已保留）。"""

    def __init__(self, message: str, *, error: str = "", path: str = "") -> None:
        super().__init__(message)
        self.error = error
        self.path = path


class SettingsNotCorruptError(RuntimeError):
    """repair 被拒：文件当前**不是损坏态**（settings-integrity 硬线）。

    场景：损坏横幅上发起的修复请求在途时，另一路（手动编辑 / 先到的
    repair）已把文件修好——晚到的 repair 若照常覆盖，会把「用户刚修好
    的设置」洗成横幅打开时抓到的旧快照。所以 repair 只允许在**发起时
    仍是损坏态**的文件上落地；健康态一律拒绝（missing 除外：无文件可
    覆盖，等同首次写）。
    """

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.error = ""
        self.path = path


class SettingsWriteError(OSError):
    """原子写本身失败（磁盘满/权限）；原文件未被触碰。"""


@dataclass
class SettingsDoc:
    """一次一致性读取的结果：数据 + 状态（不存在与损坏必须可区分）。"""

    status: str = STATUS_MISSING
    data: dict = field(default_factory=dict)
    error: str = ""  # corrupt 时的解析错误（safe to show: 异常文本）
    path: Path = field(default_factory=paths.settings_path)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def corrupt(self) -> bool:
        return self.status == STATUS_CORRUPT


def load_doc() -> SettingsDoc:
    """三态读取（锁内快照；读路径无写副作用）。

    顶层不是 JSON object（如 ``[]``/``3``）也算 corrupt——写入口只会写
    object，非 object 说明文件被动过或损坏。
    """
    with _LOCK:
        p = paths.settings_path()
        doc = SettingsDoc(path=p)
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return doc
        except OSError as e:
            doc.status = STATUS_CORRUPT
            doc.error = f"OSError: {e}"
            return doc
        if not raw.strip():
            # 零字节/纯空白：无信息可保留，与「不存在」同语义（可写默认；
            # ensure_layout 对 size==0 也是写默认，保持一致）。
            return doc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            doc.status = STATUS_CORRUPT
            doc.error = f"{type(e).__name__}: {e}"
            return doc
        if not isinstance(data, dict):
            doc.status = STATUS_CORRUPT
            doc.error = "settings.json is not a JSON object"
            return doc
        doc.status = STATUS_OK
        doc.data = data
        return doc


def read_settings() -> dict:
    """宽容读：missing/corrupt → ``{}``。

    供消费型读点（lang/classifier/env_inject 等）保持旧行为；需要区分
    三态或做写前检查的调用方必须用 :func:`load_doc`。
    """
    return dict(load_doc().data)


def is_corrupt() -> bool:
    return load_doc().corrupt


def atomic_write(data: dict) -> None:
    """原子落盘（missing/ok 状态下）。corrupt → :class:`SettingsCorruptError`。

    同目录 tmp → fsync → chmod 0600 → ``os.replace``；replace 失败时清理
    tmp 并抛 :class:`SettingsWriteError`，原文件不动。
    """
    with _LOCK:
        _assert_not_corrupt()
        _atomic_write_locked(data)


def _assert_not_corrupt() -> None:
    doc = load_doc()
    if doc.corrupt:
        raise SettingsCorruptError(
            "settings.json is corrupt; write refused to preserve original bytes",
            error=doc.error,
            path=str(doc.path),
        )


def _atomic_write_locked(data: dict) -> None:
    p = paths.settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SettingsWriteError(f"cannot create settings dir: {e}") from e
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, _FILE_MODE)
        except OSError:
            pass  # Windows / exotic FS: best-effort
        os.replace(tmp, p)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SettingsWriteError(f"atomic write failed: {e}") from e
    try:
        dfd = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # 目录 fsync 不可用的平台（best-effort 持久化）


def update(mutator: Callable[[dict], dict | None]) -> dict:
    """锁内读-改-写：``mutator(doc)`` 返回新 doc（或 None = 无变化）。

    corrupt → :class:`SettingsCorruptError`（拒写保留原字节）；missing →
    从 ``{}`` 起步（首次写）。返回实际落盘的 doc。所有「改 settings 某个
    子块」的写入口必须走这里，而不是自己 read + write（否则绕开锁与
    损坏检查，丢更新/洗数据双风险）。
    """
    with _LOCK:
        _assert_not_corrupt()
        doc = load_doc()
        new = mutator(dict(doc.data))
        if new is None:
            return doc.data
        _atomic_write_locked(new)
        return new


def backup(tag: str = "") -> Path:
    """非破坏备份：原字节复制到旁路文件，原文件不动。

    损坏恢复流程的第一步（UI 提示用户先备份再手动修复）。备份文件名
    ``settings.json.bak-<timestamp>[-<tag>]``。

    硬线（settings-integrity 复核补强）：

    - **创建瞬间 0600**：``O_CREAT | O_EXCL`` 且 mode 直接传 ``0600``
      （umask 只会收紧不会放宽）——不存在「先 0644 再 chmod」的秘密
      落地窗口；
    - **排他创建**：``O_EXCL`` 让「存在性检查 → 写入」不可分——两个
      进程同时备份永不互相覆盖（旧 ``while exists + write_bytes`` 是
      TOCTOU：都能通过检查，后写者截断覆盖前者的半成品），撞名时加
      序号重试；
    - **失败零残留**：写中途任何 OSError → 删除半成品再抛
      :class:`SettingsWriteError`，绝不留下一个「看似可用」的不完整
      备份（恢复流程靠它保底，半截备份比没有更危险）；fsync 保证崩溃
      后落盘的备份是完整的。
    """
    with _LOCK:
        p = paths.settings_path()
        try:
            raw = p.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as e:
            raise SettingsWriteError(f"cannot read settings.json for backup: {e}") from e
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{tag}" if tag else ""
        base = p.with_name(f"{p.name}.bak-{stamp}{suffix}")
        target = base
        i = 1
        while True:
            try:
                # 排他 + 创建即 0600：mode 在创建 syscall 里给出，umask
                # 只收紧；不存在事后 chmod 的 0644 窗口。
                fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
                )
                break
            except FileExistsError:
                if i > 999:
                    raise SettingsWriteError(
                        f"backup name collision exhausted under {base.parent}"
                    ) from None
                target = base.with_name(f"{base.name}.{i}")
                i += 1
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            # 零残留：半成品备份比没有更危险（恢复流程把它当保底）。
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise SettingsWriteError(
                f"backup write failed (partial file removed): {e}"
            ) from e
        return target


def repair(data: dict) -> dict:
    """显式修复（用户确认的覆盖写）：**仅限当前仍是损坏态**的文件。

    这是唯一的损坏态写通道（``PUT /api/settings/repair``）：普通 PUT 在
    损坏态一律 409，防止前端把「读到的空/坏文档」盲目 round-trip 回去；
    修复必须由用户在损坏横幅上显式发起。

    状态分派（settings-integrity 复核补强）：

    - ``corrupt`` → 先自动备份原字节（``*-pre-repair``，非破坏）再落盘；
    - ``missing`` → 等同 atomic_write（无文件可覆盖，无备份动作）；
    - ``ok`` → :class:`SettingsNotCorruptError`。旧实现把 ok 态等同
      atomic_write 放行，等于给「横幅打开时抓到的旧快照」开了一条
      覆盖健康设置的通道（手动修复/先到 repair 之后，迟到的 repair
      会静默洗掉新内容）——这是要堵的洞。

    返回 ``{"backup": path | None}``。
    """
    with _LOCK:
        doc = load_doc()
        if doc.ok:
            raise SettingsNotCorruptError(
                "settings.json is not corrupt; stale repair refused "
                "(file was fixed by a newer action — reload before editing)",
                path=str(doc.path),
            )
        backup_path: Path | None = None
        if doc.corrupt:
            backup_path = backup(tag="pre-repair")
        _atomic_write_locked(data)
        return {"backup": backup_path}


def health() -> dict:
    """健康快照（GET /api/settings/health 的数据源）。"""
    doc = load_doc()
    out: dict = {"status": doc.status, "error": doc.error or None, "path": str(doc.path)}
    try:
        st = doc.path.stat()
        out["size"] = st.st_size
        out["mtime"] = st.st_mtime
    except OSError:
        pass
    return out
