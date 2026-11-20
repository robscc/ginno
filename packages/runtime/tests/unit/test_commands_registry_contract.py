"""Built-in slash-command registry contract snapshot（重构批次 0）.

``commands/registry.py`` 的 ``BUILTINS`` 后续要拆成命令包；本文件是拆包的
验收基准：命令清单（名称 / 双语描述 / sync-async 路由 / 通道策略 /
handler 签名）与用户面帮助文案（``/help`` 输出 + 各命令空参回复）
**逐字节零漂移**。

任何对 registry.py 文案或注册项的改动都会让本测试红——这是设计意图：
有意变更时用下面的命令再生成快照，并逐行审阅 diff。

再生成（会覆盖快照文件）::

    cd packages/runtime
    GINNO_UPDATE_SNAPSHOT=1 uv run --group test --extra docs \
        pytest -q tests/unit/test_commands_registry_contract.py

快照生成环境与普通跑测一致（根 conftest 的 ``isolated_home`` autouse
fixture：GINNO_HOME=临时目录），因此 ``/help`` 的 skills 段只含包内
builtin skills（browser / todo），不含开发机的全局/项目 skills——快照
与机器无关、CI 与本地逐字节一致。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest

from ginno_runtime.commands.registry import BUILTINS
from ginno_runtime.lang import bind_request_locale, reset_request_locale, t

pytestmark = pytest.mark.unit

SNAPSHOT_PATH = Path(__file__).with_name("commands_registry_snapshot.txt")
_UPDATE_ENV = "GINNO_UPDATE_SNAPSHOT"
_REGEN_CMD = (
    f"cd packages/runtime && {_UPDATE_ENV}=1 "
    "uv run --group test --extra docs pytest -q "
    "tests/unit/test_commands_registry_contract.py"
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_VALID_POLICIES = {"all", "admin", "none"}
_LOCALES = ("en", "zh-CN")
_HANDLER_KWS = ("project_slug", "session", "args")


@contextmanager
def _bound_locale(locale: str) -> Iterator[None]:
    token = bind_request_locale(locale)
    try:
        yield
    finally:
        reset_request_locale(token)


def _desc(cmd: object, locale: str) -> str:
    with _bound_locale(locale):
        return t(*cmd.description)  # type: ignore[arg-type]


def _render_help(locale: str) -> str:
    """真实 help handler 的输出（单一事实源，不在测试里复刻拼接逻辑）。"""
    with _bound_locale(locale):
        return BUILTINS["help"].handler(project_slug=None, session=None, args=None)


def _no_args_reply(name: str, locale: str) -> str:
    """空参、无会话上下文时各命令 sync handler 的回复——即该命令的
    usage/参数说明文案（goal/mount/subagent/think 等返回整段用法；
    compact/clear/fork/workflow-* 返回异步入口提示）。全部纯文案、无副作用。"""
    with _bound_locale(locale):
        return BUILTINS[name].handler(project_slug=None, session=None, args=None)


def _text_block(label: str, value: str, out: list[str]) -> None:
    out.append(f"{label}: |")
    out.extend(f"  {ln}" if ln else "" for ln in value.split("\n"))


def _command_block(name: str) -> list[str]:
    cmd = BUILTINS[name]
    sync: Callable = cmd.handler
    out = [f"[/{name}]"]
    out.append(f"channel_policy: {cmd.channel_policy}")
    out.append(f"sync_handler: {getattr(sync, '__qualname__', repr(sync))}")
    out.append(f"sync_signature: {inspect.signature(sync)}")
    if cmd.async_handler is not None:
        out.append(f"async_handler: {cmd.async_handler.__qualname__}")
        out.append(f"async_signature: {inspect.signature(cmd.async_handler)}")
    else:
        out.append("async_handler: -")
    for locale in _LOCALES:
        out.append(f"description.{locale}: {_desc(cmd, locale)}")
    for locale in _LOCALES:
        _text_block(f"no_args_reply.{locale}", _no_args_reply(name, locale), out)
    return out


def _render_snapshot() -> str:
    out: list[str] = [
        "# Ginno 内置斜杠命令注册表契约快照（commands/registry.py 重构批次 0 验收基准）。",
        "# 目的：拆包期间命令清单与文案零漂移——本文件被测试逐字节断言，",
        "# 任何 diff 都必须是有意变更并进 PR 审阅。",
        "# 再生成（会覆盖本文件；生成后逐行审阅 git diff）：",
        f"#   {_REGEN_CMD}",
        "# 生成环境：根 conftest isolated_home autouse fixture（GINNO_HOME=临时目录），",
        "# /help 的 skills 段只含包内 builtin skills，与开发机/CI 的机器状态无关。",
        "",
        f"command_count: {len(BUILTINS)}",
        "",
        "== /help 输出 ==",
    ]
    for locale in _LOCALES:
        out.append("")
        _text_block(f"locale.{locale}", _render_help(locale), out)
    out += ["", "== 命令清单（按名称排序）=="]
    for name in sorted(BUILTINS):
        out.append("")
        out += _command_block(name)
    return "\n".join(out) + "\n"


def test_registry_snapshot_matches_byte_for_byte() -> None:
    body = _render_snapshot()
    if os.environ.get(_UPDATE_ENV):
        SNAPSHOT_PATH.write_text(body, encoding="utf-8")
        pytest.skip("快照已再生成——审阅 git diff 后随改动一并提交")
    assert SNAPSHOT_PATH.exists(), (
        f"快照缺失。先再生成并一并提交：{_REGEN_CMD}"
    )
    assert SNAPSHOT_PATH.read_bytes() == body.encode("utf-8"), (
        "命令注册表契约漂移（清单/描述/路由/帮助文案变了）。有意变更时再生成快照"
        f"并审阅 diff：{_REGEN_CMD}；否则恢复改动。"
    )


def test_registry_entries_wellformed() -> None:
    """注册表结构不变量（不依赖快照，红的时候给出语义化报错）。"""
    assert BUILTINS, "注册表为空——拆包/重构把 BUILTINS 弄丢了"
    for name, cmd in BUILTINS.items():
        assert _NAME_RE.match(name), f"/{name}: 命令名应为小写 token（^a-z0-9-）"
        assert isinstance(cmd.description, tuple) and len(cmd.description) == 2, (
            f"/{name}: description 必须是 (en, zh) 二元组"
        )
        assert all(isinstance(s, str) and s.strip() for s in cmd.description), (
            f"/{name}: 描述两个语言变体都必须非空"
        )
        assert cmd.channel_policy in _VALID_POLICIES, f"/{name}: 非法 channel_policy"
        assert callable(cmd.handler), f"/{name}: handler 必须可调用"
        if cmd.async_handler is not None:
            assert asyncio.iscoroutinefunction(cmd.async_handler), (
                f"/{name}: async_handler 必须是协程函数"
            )
        fns = [cmd.handler] + ([cmd.async_handler] if cmd.async_handler else [])
        for fn in fns:
            params = inspect.signature(fn).parameters
            has_var = any(
                p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for p in params.values()
            )
            for kw in _HANDLER_KWS:
                ok = has_var or (
                    kw in params
                    and params[kw].kind
                    in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                )
                assert ok, (
                    f"/{name}: handler 必须接受 {kw}= 关键字"
                    "（resolver / WS invoke / channel 调用面的契约）"
                )
