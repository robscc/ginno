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

# 向导步骤契约(i18n-design.md §3):title/body 是英文兜底;i18n_key 指向 web
# catalog 的 conn.wizard.step.* 键(前端按 <key>.title / <key>.body 翻译)。
CHROME_INSTALL_STEPS = [
    {
        "key": "intro",
        "i18n_key": "conn.wizard.step.intro",
        "title": "What is this",
        "body": ("Ginno can work for you inside your own Chrome — using your "
                 "logged-in sessions, clicking and typing like you would. You "
                 "can always see what it's doing and stop it at any time. "
                 "It needs a one-time install of a Ginno browser extension "
                 "(about 1 minute)."),
    },
    {
        "key": "folder",
        "i18n_key": "conn.wizard.step.folder",
        "title": "Extension files are ready",
        "body": ("Ginno has placed the extension files in the "
                 "~/.ginno/browser-extension folder on your computer. Keep "
                 "this folder open — you'll need it in step 4."),
        "action": "reveal_folder",
    },
    {
        "key": "open_extensions",
        "i18n_key": "conn.wizard.step.open_extensions",
        "title": "Open the Chrome extensions page",
        "body": ("Type the address below into Chrome's address bar and press "
                 "Enter (browsers don't allow web pages to open this page "
                 "directly, so you need to type it yourself):"),
        "copy": "chrome://extensions",
    },
    {
        "key": "dev_mode",
        "i18n_key": "conn.wizard.step.dev_mode",
        "title": "Enable Developer mode",
        "body": "There's a \"Developer mode\" toggle in the top-right corner of the extensions page — turn it on.",
    },
    {
        "key": "load",
        "i18n_key": "conn.wizard.step.load",
        "title": "Load the extension",
        "body": ("Click \"Load unpacked\" in the top-left, then select the "
             "browser-extension folder you just opened in the folder picker "
             "(note: select the folder itself, don't go inside it), and "
             "click \"Select\". Seeing \"Ginno\" appear in the list means "
             "success."),
    },
    {
        "key": "wait",
        "i18n_key": "conn.wizard.step.wait",
        "title": "Confirm connection",
        "body": "Waiting for the extension to connect back to Ginno…",
        "waitConnect": True,
    },
]


class ChromeExtensionConnector(Connector):
    id = "chrome-extension"
    name = "Chrome Browser Extension"
    description = "Let Ginno work in your own Chrome: shared logins, visible actions, always under your control."
    icon = "globe"
    order = 10
    # 与 browser-profile 同前缀:浏览器能力 = 两条轨的并集(设计 §8)
    tool_prefix = "browser_"

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
                "enabled": {"type": "boolean", "title": "Enabled"},
                "fallback_profile_mode": {
                    "type": "string", "enum": ["ask", "auto", "off"],
                    "title": "Fallback browser when extension is missing",
                    "description": ("ask=prompt once before using / auto=silently "
                                    "use Ginno's built-in Chrome profile / "
                                    "off=extension track only"),
                },
                "tab_scope": {
                    "type": "string", "enum": ["group", "all"],
                    "title": "Tab visibility",
                    "description": ("group=only tabs in the Ginno group (fenced; "
                                    "the agent can't see your personal tabs) / "
                                    "all=every tab in the browser (readable and "
                                    "controllable; mind your privacy)"),
                },
                "sensitive_domains": {
                    "type": "array", "items": {"type": "string"},
                    "title": "Protected domains",
                    "description": "Browser actions on these domains require your confirmation (payments / email / cloud consoles)",
                },
                "confirmed_domains": {
                    "type": "array", "items": {"type": "string"},
                    "title": "Confirmed protected domains",
                    "description": ("You've allowed the agent to operate on these "
                                    "protected domains (added here after the agent "
                                    "asks; one per line)"),
                },
                "auto_open_group": {
                    "type": "boolean", "title": "Bring Chrome to front on new tabs",
                },
            },
        }

    def install_steps(self) -> list[dict]:
        """folder 步骤的扩展目录路径在调用时解析(GINNO_HOME 可被环境改写),
        以 params 占位下发——契约字段同 i18n-design.md §3(params 可选)。"""
        from ..browser.config import extension_dir

        return [
            {**step, "params": {"path": str(extension_dir())}}
            if step["key"] == "folder" else step
            for step in CHROME_INSTALL_STEPS
        ]

    async def apply_config(self, cfg: dict) -> None:
        """配置变更即时下发扩展(tab_scope 等无需重连)。"""
        try:
            from ..browser.relay import push_config
            from ..lang import current_locale

            await push_config({
                "tabScope": cfg.get("tab_scope", "group"),
                # Model-facing tool errors in the extension follow the runtime
                # locale. The extension's vocabulary is "en" / "zh" only
                # (background.js ignores anything else), so map zh-CN → zh.
                "lang": "zh" if current_locale() == "zh-CN" else "en",
            })
        except Exception:  # noqa: BLE001 — 推送失败无妨,重连时补推
            pass


class BrowserProfileConnector(Connector):
    id = "browser-profile"
    name = "Ginno Browser Profile"
    description = ("A Ginno-managed Chrome instance (a dedicated profile, isolated "
                   "from your own browser logins). The fallback track when the "
                   "extension isn't installed.")
    icon = "monitor"
    order = 20
    tool_prefix = "browser_"

    def default_config(self) -> dict:
        return {"enabled": True, "headless": False}

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "title": "Enabled"},
                "headless": {"type": "boolean", "title": "Headless mode",
                             "description": "Don't show a browser window (off by default: actions stay visible and interruptible)"},
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
        reg.report("browser-profile", STATUS_DISCONNECTED, "Not running (starts on demand)",
                   extra={"idle": True})


__all__ = [
    "ChromeExtensionConnector",
    "BrowserProfileConnector",
    "ensure_builtin_connectors",
    "STATUS_CONNECTED",
    "STATUS_DISCONNECTED",
    "STATUS_NOT_INSTALLED",
]
