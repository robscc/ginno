"""菜单/命令：匹配、模板渲染与帮助文案（设计 F8）。

F8 流程：F2 步骤 3 收到文本后先 ``is_builtin``（/help、菜单 → 回复帮助），
再 ``match``；命中则用 ``render`` 把模板变量替换后的结果作为 user_text
进入 F2 步骤 7（``new_session=true`` 的菜单强制走 F2 步骤 4 新开会话）。

菜单配置即 §2.2 连接器 config 的 ``menus`` 列表（单一权威，钉钉侧除
``/help``/``菜单`` 外无硬编码命令）。纯本地逻辑，不碰网络 API。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 内置命令（F8 步骤 5）：触发帮助文案，不进入菜单匹配。
_BUILTIN_TRIGGERS = frozenset({"/help", "菜单"})


@dataclass
class MenuItem:
    """一条菜单/命令配置（§2.2 ``menus.item_schema``）。"""

    trigger: str
    """触发词：用户输入与其全等、或 ``/<trigger>`` 前缀即命中（大小写不敏感）。"""

    title: str
    """展示名（帮助列表用）。"""

    prompt: str
    """注入的指令模板，支持 ``{date}/{time}/{staff_name}`` 变量。"""

    new_session: bool = False
    """True → 每次命中强制新开 ginno 会话（F2 步骤 4）。"""


def load_menus(config: dict) -> list[MenuItem]:
    """从连接器 config 的 ``menus`` 列表构建 :class:`MenuItem`。

    坏项（非 dict、缺 trigger/title/prompt、类型不符）逐项跳过——
    用户手改配置出错时，渠道按剩余合法菜单继续工作。
    """
    raw = config.get("menus")
    if not isinstance(raw, list):
        return []
    items: list[MenuItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        trigger = entry.get("trigger")
        title = entry.get("title")
        prompt = entry.get("prompt")
        if not all(isinstance(v, str) and v for v in (trigger, title, prompt)):
            continue
        items.append(
            MenuItem(
                trigger=trigger,
                title=title,
                prompt=prompt,
                new_session=bool(entry.get("new_session", False)),
            )
        )
    return items


def match(menus: list[MenuItem], text: str) -> MenuItem | None:
    """按用户输入匹配菜单：与 trigger 全等、或 ``/<trigger>`` 前缀。

    text 先去首尾空白；trigger 比较大小写不敏感。未命中返回 None
    （F2 按"普通对话"处理）。遍历顺序即配置顺序，首个命中生效。
    """
    t = text.strip().lower()
    if not t:
        return None
    for item in menus:
        trigger = item.trigger.strip().lower()
        if t == trigger or t.startswith("/" + trigger):
            return item
    return None


def render(
    item: MenuItem,
    *,
    staff_name: str = "",
    date: str | None = None,
    time: str | None = None,
) -> str:
    """渲染 prompt 模板：替换 ``{date}/{time}/{staff_name}``。

    ``date``/``time`` 缺省取当前本地时间（``%Y-%m-%d`` / ``%H:%M``）。
    用逐变量 replace 而非 ``str.format``：模板里未知的 ``{xxx}``
    原样保留，不抛 KeyError。
    """
    now = datetime.now()
    return (
        item.prompt.replace("{date}", date if date is not None else now.strftime("%Y-%m-%d"))
        .replace("{time}", time if time is not None else now.strftime("%H:%M"))
        .replace("{staff_name}", staff_name)
    )


def help_markdown(menus: list[MenuItem]) -> str:
    """帮助文案（F8 步骤 5）：markdown 列表，含每个菜单的触发词与标题。

    空菜单时提示直接对话，避免输出一张空列表。
    """
    if not menus:
        return "暂无可用菜单，直接发消息即可对话。"
    lines = ["**可用菜单**"]
    lines += [f"- `/{item.trigger}` — {item.title}" for item in menus]
    return "\n".join(lines)


def is_builtin(text: str) -> bool:
    """是否内置命令：``/help`` 或 ``菜单``（去首尾空白，大小写不敏感）。"""
    return text.strip().lower() in _BUILTIN_TRIGGERS
