"""Secret 卫生(ginno-open-dynamic-workflow-comparison.html P1-②,ODW redactEnv
+ passEnv 的 Ginno 适配)。

Ginno 的暴露面与 ODW 不同:workflow 引擎本身不碰 ``os.environ``,secret 从
三个口进来——schedule/创建 run 的 context_override、step 工具输出、
delegate_agent 子进程继承的 env。对应三道闸:

1. **落盘前值级脱敏**(本模块主战场):run JSON / events.jsonl 写入前,
   递归把「名字长得像 secret 的键」的值替换为 ``[REDACTED:name]``;对
   已知格式的高熵值(sk-/ghp_/xox/AKIA/Bearer 前缀)做值级兜底。展示层
   (inspector/doctor)读的就是落盘文件,写侧脱敏即覆盖读侧。
2. **子进程 env**:delegate_agent 的 CLI 需要自己的 key 才能跑
   (ANTHROPIC_API_KEY/OPENAI_API_KEY…),ODW 的「passEnv 默认空白名单」
   在 Ginno 会直接开箱坏——**刻意不采用**:子进程继承保持全量,但 env
   值一旦进入 meta/事件/转录(``redact_env`` 供这些点调用)即脱敏。
   设计偏差记录于对比报告补充(2026-10-23)。
3. **doctor/诊断输出**:经 ``redact_env`` 打印。

不可逆替换(写侧替换原值,非展示掩码)是刻意的:落盘文件就是共享面,
留原文等于没脱。代价是排障时看不到原值——重跑可复现,可接受。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 名字级:键名以 secret 词族**单数**结尾(大小写不敏感)即脱敏。ODW 默认
# *_KEY/*_TOKEN/*_SECRET/PASSWORD;Ginno 再补 auth/credential 两族(连接器/
# 钉钉场景常见)。刻意保守(宁漏勿错——误伤数据比漏一个可重置的 key 更伤):
# - 不收复数:``input_tokens``/``output_tokens`` 这类用量计数键以 _tokens
#   结尾,收复数会把全部 usage 事件脱成 [REDACTED](2026-10-23 实测翻车);
#   ``SECRETS`` 复数键因此漏掉,可接受。
# - 前缀命中不脱:``token_count``/``api_key_hash``。
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:password|passwd|secret|token|api[_-]?key|auth|credential)$"
)

# 值级兜底:不带键名的裸 secret(工具输出里粘贴的 key)。只认高置信前缀,
# 宁漏勿错——误伤普通文本比漏一个 key 更伤。
_SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgho_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}"),
)

_REDACTED = "[REDACTED]"

# 深度/宽度帽:递归脱敏走的是待落盘的 JSON 树,病态嵌套不炸栈。
_MAX_DEPTH = 24
_MAX_ITEMS = 20_000


def is_secret_key(key: str) -> bool:
    """键名是否命中 secret 名字规则。``APIKEY``/``api_key``/``AUTH_TOKEN``
    一族全中;``keyboard``/``authority`` 这类假阳性被 ``(?:$|_)`` 边界挡掉。"""
    k = (key or "").strip()
    if not k or len(k) > 128:
        return False
    return bool(_SECRET_KEY_RE.search(k))


def redact_text(text: str) -> str:
    """值级兜底:裸 secret 前缀替换。"""
    out = text
    for rex in _SECRET_VALUE_RES:
        out = rex.sub(_REDACTED, out)
    return out


def redact(value: Any, *, depth: int = 0, items: int = 0) -> Any:
    """递归脱敏一棵待落盘的 JSON 树(dict 按键名、str 按值格式)。

    返回**新结构**(浅层复制,不改原对象——原对象可能还要在内存里用);
    非 str/dict/list 原样返回。超深度/超宽度的子树原样保留(脱敏是卫生
    层,不是校验层,截断反而藏错)。"""
    if depth > _MAX_DEPTH or items > _MAX_ITEMS:
        return value
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            items += 1
            out[str(k)] = _REDACTED if is_secret_key(str(k)) else redact(v, depth=depth + 1, items=items)
        return out
    if isinstance(value, list):
        return [redact(v, depth=depth + 1, items=items + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_env(env: dict | None = None) -> dict:
    """env 映射的展示形(secret 键 → [REDACTED]),doctor/诊断打印用。"""
    src = dict(os.environ if env is None else env)
    return {k: (_REDACTED if is_secret_key(k) else v) for k, v in src.items()}
