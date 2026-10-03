"""The built-in connectors.

- ``chrome-extension``: 扩展轨 — status is driven by the relay endpoint
  (browser/relay.py). WS 已连 > native messaging 已连 > 都没有 (设计 §7.3
  的判定顺序:避免把网络抖动误报成安装问题)。
- ``browser-profile``: B 轨 — Ginno 自管的专用 profile Chrome 实例
  (browser/executor.ProfileBackend),按需启动。
"""

from __future__ import annotations

from .registry import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_NOT_INSTALLED,
    Connector,
)

CHROME_INSTALL_STEPS = [
    {
        "key": "intro",
        "title": "这是什么",
        "body": ("Ginno 可以在你自己的 Chrome 里替你干活——用你的登录态,"
                 "像你一样点按输入。你随时能看到它在动,也可以随时叫停。"
                 "需要装一个 Ginno 的浏览器扩展(一次性,约 1 分钟)。"),
    },
    {
        "key": "folder",
        "title": "扩展已就位",
        "body": ("Ginno 已将扩展文件放到你电脑上的 ~/.ginno/browser-extension 文件夹。"
                 "保持此文件夹打开,第 4 步要用。"),
        "action": "reveal_folder",
    },
    {
        "key": "open_extensions",
        "title": "打开 Chrome 扩展页",
        "body": ("在 Chrome 地址栏输入下面的地址并回车(浏览器不允许网页直接打开"
                 "这个页面,需要你手动输入):"),
        "copy": "chrome://extensions",
    },
    {
        "key": "dev_mode",
        "title": "开启开发者模式",
        "body": "扩展页右上角有「开发者模式」开关,打开它。",
    },
    {
        "key": "load",
        "title": "加载扩展",
        "body": ("点击左上角「加载已解压的扩展程序」,在弹出的文件夹选择器里选中刚才"
             "打开的 browser-extension 文件夹(注意:选中文件夹本身,不是进去选里面"
             "的文件),点「选择」。列表里出现「Ginno」即成功。"),
    },
    {
        "key": "wait",
        "title": "连接确认",
        "body": "正在等待扩展连回 Ginno…",
        "waitConnect": True,
    },
]


class ChromeExtensionConnector(Connector):
    id = "chrome-extension"
    name = "Chrome 浏览器扩展"
    description = "用你自己的 Chrome 替你干活:登录态共享、动作可见、可接管。"
    icon = "globe"
    order = 10

    def default_config(self) -> dict:
        return {
            "enabled": True,
            # B 轨启用方式(设计 §3): ask / auto / off
            "fallback_profile_mode": "ask",
            "sensitive_domains": [],
            "auto_open_group": True,
            # tab 可见范围: group=仅 Ginno 组(围栏,默认) / all=全部 tab
            "tab_scope": "group",
        }

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "title": "启用"},
                "fallback_profile_mode": {
                    "type": "string", "enum": ["ask", "auto", "off"],
                    "title": "未装扩展时的备用浏览器",
                    "description": ("ask=提示一次后启用 / auto=静默使用 Ginno 自带的"
                                    " Chrome 实例 / off=仅扩展轨"),
                },
                "tab_scope": {
                    "type": "string", "enum": ["group", "all"],
                    "title": "tab 可见范围",
                    "description": ("group=仅 Ginno 组内的 tab(围栏,agent 看不到你"
                                    "个人页面)/ all=浏览器全部 tab(可读可操作,"
                                    "注意隐私)"),
                },
                "sensitive_domains": {
                    "type": "array", "items": {"type": "string"},
                    "title": "受保护域名",
                    "description": "这些域名上的浏览器动作需要你确认(支付/邮箱/云控制台)",
                },
                "confirmed_domains": {
                    "type": "array", "items": {"type": "string"},
                    "title": "受保护域名确认",
                    "description": ("你已同意 agent 在这些受保护域名上操作"
                                    "(agent 请求确认后在此加入;每行一个)"),
                },
                "auto_open_group": {
                    "type": "boolean", "title": "新标签时把 Chrome 带到前台",
                },
            },
        }

    def install_steps(self) -> list[dict]:
        return CHROME_INSTALL_STEPS

    async def apply_config(self, cfg: dict) -> None:
        """配置变更即时下发扩展(tab_scope 等无需重连)。"""
        try:
            from ..browser.relay import push_config

            await push_config({"tabScope": cfg.get("tab_scope", "group")})
        except Exception:  # noqa: BLE001 — 推送失败无妨,重连时补推
            pass


class BrowserProfileConnector(Connector):
    id = "browser-profile"
    name = "Ginno 浏览器实例"
    description = ("Ginno 自管的 Chrome 实例(专用配置目录,与你的浏览器登录态隔离)。"
                   "未装扩展时的备用轨道。")
    icon = "monitor"
    order = 20

    def default_config(self) -> dict:
        return {"enabled": True, "headless": False}

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "title": "启用"},
                "headless": {"type": "boolean", "title": "无头模式",
                             "description": "不显示浏览器窗口(默认关闭:操作可见、可接管)"},
            },
        }


def ensure_builtin_connectors() -> None:
    from .registry import registry

    reg = registry()
    reg.register(ChromeExtensionConnector())
    reg.register(BrowserProfileConnector())
    # 初始状态:扩展未安装;profile 轨道按需启动,先标 disconnected(未运行)
    if reg.status_of("chrome-extension")["status"] == STATUS_NOT_INSTALLED:
        pass  # stays not_installed until relay says otherwise
    if reg.status_of("browser-profile")["status"] == STATUS_NOT_INSTALLED:
        reg.report("browser-profile", STATUS_DISCONNECTED, "未运行(按需启动)",
                   extra={"idle": True})


__all__ = [
    "ChromeExtensionConnector",
    "BrowserProfileConnector",
    "ensure_builtin_connectors",
    "STATUS_CONNECTED",
    "STATUS_DISCONNECTED",
    "STATUS_NOT_INSTALLED",
]
