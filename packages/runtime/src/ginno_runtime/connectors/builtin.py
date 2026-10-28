"""The built-in connectors.

- ``chrome-extension``: 扩展轨 — status is driven by the relay endpoint
  (browser/relay.py). WS 已连 > native messaging 已连 > 都没有 (设计 §7.3
  的判定顺序:避免把网络抖动误报成安装问题)。
- ``browser-profile``: B 轨 — Ginno 自管的专用 profile Chrome 实例
  (browser/executor.ProfileBackend),按需启动。
"""

from __future__ import annotations

import logging

from .registry import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_NOT_INSTALLED,
    Connector,
)

log = logging.getLogger("ginno.connectors")

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
                    "title": "Fallback browser when extension missing",
                    "description": ("ask=prompt once before using / auto=silently "
                                    "use Ginno's built-in Chrome profile / "
                                    "off=extension track only"),
                },
                "tab_scope": {
                    "type": "string", "enum": ["group", "all"],
                    "title": "Tab visibility",
                    "description": ("group=only tabs in the Ginno group "
                                    "(fenced; no personal tabs visible) / "
                                    "all=every tab (readable and "
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


class WeComConnector(Connector):
    """企业微信智能机器人渠道连接器（docs/wecom-connector-design.md）。

    Connector 只负责配置/安装引导/状态；协议对接在
    ``channels/wecom/adapter.py``（WS 长连接 + 流式回复）。与钉钉不同：
    - 凭证只有 BotID + Secret 两个（长连接模式，无回调 URL/加解密配置）；
    - 启动恢复：apply_config 只在 PATCH 时触发，seed 时补一次自动拉起
      （钉钉 P0 已知缺口，企微实现直接补上）。
    """

    id = "wecom-bot"
    name = "WeCom Smart Bot"
    description = ("Chat with Ginno inside WeCom (企业微信) via a smart bot's "
                   "long connection: streaming replies, text answers to agent "
                   "questions, file and image exchange — your desktop Ginno "
                   "stays the runtime.")
    icon = "message-circle"
    order = 35
    # 入站渠道,不向 agent 提供 tools
    tool_prefix = None

    def default_config(self) -> dict:
        return {
            "enabled": False,
            "bot_id": "",
            "secret": "",
            "allowed_userids": [],
            "default_agent_id": "",
            "session_policy": "per_user",
            "ask_timeout_s": 600,
            "welcome_text": "你好，我是 Ginno，直接发消息即可开始。",
        }

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "title": "Enabled"},
                "bot_id": {
                    "type": "string", "title": "BotID",
                    "description": ("From the WeCom admin console: 智能机器人 → "
                                    "API 模式 → 长连接"),
                },
                "secret": {"type": "string", "title": "Secret",
                           "format": "password"},
                "allowed_userids": {
                    "type": "array", "items": {"type": "string"},
                    "title": "Allowed userids",
                    "description": ("Empty = everyone who can reach the bot "
                                    "can drive the agent"),
                },
                "default_agent_id": {
                    "type": "string", "title": "Default agent for this channel",
                },
                "session_policy": {
                    "type": "string", "enum": ["per_user", "per_conversation"],
                    "title": "Session policy",
                    "description": ("per_user: each user gets their own session "
                                    "per chat; per_conversation: one shared "
                                    "session per chat (groups)"),
                },
                "ask_timeout_s": {
                    "type": "integer", "title": "ask_user timeout (seconds)",
                    "description": "Auto-skip a parked question after this",
                },
                "welcome_text": {
                    "type": "string", "title": "Welcome message",
                    "description": ("Sent when a user enters the chat "
                                    "(enter_chat event); empty = disabled"),
                },
            },
        }

    def install_steps(self) -> list[dict]:
        return [
            {
                "key": "intro",
                "title": "What is this",
                "body": ("Ginno can be reached from your WeCom app: you chat "
                         "with the same agent, replies stream into the chat, "
                         "and your desktop Ginno keeps running the turns."),
            },
            {
                "key": "create_bot",
                "title": "Create a smart bot in API mode",
                "body": ("Log in to the WeCom admin console (work.weixin.qq.com) "
                         "→ 应用管理 → 智能机器人 → 创建, choose API 模式 and "
                         "set the connection method to 使用长连接. No public "
                         "callback URL is needed."),
                "copy": "https://work.weixin.qq.com",
            },
            {
                "key": "creds",
                "title": "Copy the credentials",
                "body": ("On the bot's API 配置 page, copy BotID and Secret. "
                         "Add the bot to a chat (单聊 or 群聊) so users can "
                         "talk to it."),
            },
            {
                "key": "fill",
                "title": "Paste the credentials here and enable",
                "body": ("Fill BotID/Secret above, optionally restrict allowed "
                         "userids, then toggle Enabled. The connector card "
                         "should show Connected within seconds."),
            },
        ]

    async def apply_config(self, cfg: dict) -> None:
        """生命周期：(re)start 或 stop 渠道适配器（同钉钉 F1）。"""
        from ..channels.wecom.adapter import WeComChannelAdapter

        global _wecom_adapter
        if not cfg.get("enabled") or not (cfg.get("bot_id") and cfg.get("secret")):
            if _wecom_adapter is not None:
                await _wecom_adapter.stop()
                _wecom_adapter = None
            return
        if _wecom_adapter is not None:
            await _wecom_adapter.stop()
        _wecom_adapter = WeComChannelAdapter()
        await _wecom_adapter.start(cfg)


_wecom_adapter = None  # singleton owned by WeComConnector.apply_config


def _wecom_maybe_autostart() -> None:
    """启动恢复：runtime 重启后按已存配置拉起长连接。

    apply_config 只在 PATCH config 时被调（vibe_island 同款坑），因此 seed
    时读 merged config 补一次；幂等（已拉起则跳过），仅在事件循环内执行。
    """
    import asyncio

    global _wecom_adapter
    if _wecom_adapter is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无循环的同步上下文（理论不到达）：跳过，等下次 API 调用
    try:
        from .registry import registry

        conn = registry().get("wecom-bot")
        cfg = registry().read_config("wecom-bot")
    except Exception:  # noqa: BLE001 — 注册表异常不影响 seed
        return
    if conn is None or not (cfg.get("enabled") and cfg.get("bot_id") and cfg.get("secret")):
        return

    async def _start() -> None:
        global _wecom_adapter
        from ..channels.wecom.adapter import WeComChannelAdapter

        if _wecom_adapter is not None:
            return
        _wecom_adapter = WeComChannelAdapter()
        await _wecom_adapter.start(cfg)

    loop.create_task(_start())


def ensure_builtin_connectors() -> None:
    from .registry import registry

    reg = registry()
    reg.register(ChromeExtensionConnector())
    reg.register(BrowserProfileConnector())
    reg.register(DingTalkConnector())
    reg.register(WeComConnector())
    reg.register(VibeIslandConnector())
    # 初始状态:扩展未安装;profile 轨道按需启动,先标 disconnected(未运行)
    if reg.status_of("chrome-extension")["status"] == STATUS_NOT_INSTALLED:
        pass  # stays not_installed until relay says otherwise
    if reg.status_of("browser-profile")["status"] == STATUS_NOT_INSTALLED:
        reg.report("browser-profile", STATUS_DISCONNECTED,
                   "Not running (starts on demand)", extra={"idle": True})
    # 钉钉:默认 disabled(enabled=False),用户配好 appKey/appSecret 并启用后,
    # apply_config 拉起 Stream 连接(F1)。
    if reg.status_of("dingtalk-bot")["status"] == STATUS_NOT_INSTALLED:
        reg.report("dingtalk-bot", STATUS_DISCONNECTED, "Not configured",
                   extra={"idle": True})
    # 企业微信:未配置时同样标 disconnected;已配置则启动恢复拉起长连接。
    if reg.status_of("wecom-bot")["status"] == STATUS_NOT_INSTALLED:
        reg.report("wecom-bot", STATUS_DISCONNECTED, "Not configured",
                   extra={"idle": True})
    _wecom_maybe_autostart()
    # Vibe Island:按 bridge 存在性 + 开关 + 应用是否在跑报初始状态
    # (vibe_island.report_status 内部读 registry config,自带 60s 冷却,
    # 这里 force 一次保证启动即准确)。
    try:
        from .. import vibe_island

        vibe_island.report_status(force=True)
    except Exception:  # noqa: BLE001 — 状态探测失败不挡连接器注册
        pass
    # 已安装的 Claude Mods 也以动态连接器实例出现(L1+L2,存储仍是
    # settings.mods)。幂等:安装/卸载集合变化时增删实例。
    try:
        from .mod_adapter import sync_mod_connectors

        sync_mod_connectors(reg)
    except Exception:  # noqa: BLE001 — mods 同步失败不挡内置连接器
        log.exception("mod connector sync failed (continuing)")


class VibeIslandConnector(Connector):
    """Vibe Island 刘海状态连接器(vibeisland.app/docs/custom-agents.md)。

    纯出站状态上报:会话生命周期事件经 ~/.vibe-island/bin/vibe-island-bridge
    送给本机 Vibe Island 应用(每事件一次子进程、2s 超时、单向 best-effort,
    映射与队列在 ``vibe_island.py``)。不向 agent 提供 tools,也不读回任何
    数据——notch 目前只展示状态,点击卡片回到 Ginno。
    """

    id = "vibe-island"
    name = "Vibe Island"
    description = ("Show Ginno sessions in the Vibe Island notch: working/idle "
                   "status, the running tool, completion notices and approval "
                   "reminders — one card per session.")
    icon = "island"
    order = 40
    # 出站状态通道,不向 agent 提供 tools
    tool_prefix = None

    def default_config(self) -> dict:
        # 默认关:需要用户装了 Vibe Island(直下载版 ≥1.0.53)才有意义。
        return {"enabled": False}

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean", "title": "Enabled",
                    "description": ("Requires the Vibe Island app (direct "
                                    "download edition, 1.0.53 or newer)"),
                },
            },
        }

    def install_steps(self) -> list[dict]:
        return [
            {
                "key": "intro",
                "title": "What is this",
                "body": ("Vibe Island shows what your AI agents are doing in "
                         "the Mac notch. With this connector, each Ginno "
                         "session gets a card there: working or idle, the "
                         "tool it is running, a completion notice when the "
                         "turn ends, and a reminder when it needs your answer "
                         "or approval."),
            },
            {
                "key": "install_app",
                "title": "Install Vibe Island",
                "body": ("Download the direct-download edition (1.0.53 or "
                         "newer) from vibeisland.app and open it once. The "
                         "Mac App Store edition does not support custom "
                         "agents yet."),
                "copy": "https://vibeisland.app",
            },
            {
                "key": "enable",
                "title": "Enable the connector",
                "body": ("Toggle Enabled above. New Ginno sessions then show "
                         "up in the notch — move the pointer to the notch to "
                         "open the panel, and click a card to jump back to "
                         "Ginno."),
            },
        ]

    async def apply_config(self, cfg: dict) -> None:
        """开关即时生效:vibe_island 模块的发送闸门 + 状态卡片刷新。启用时
        顺带写 manifest(displayName/bundleIdentifier,指南 Step 5)。"""
        from .. import vibe_island

        enabled = bool(cfg.get("enabled"))
        vibe_island.set_enabled(enabled)
        if enabled:
            vibe_island.ensure_manifest()
        vibe_island.report_status(force=True)


class DingTalkConnector(Connector):
    """钉钉机器人渠道连接器(docs/dingtalk-connector-design.md)。

    Connector 只负责配置/安装引导/状态;协议对接在
    ``channels/dingtalk/adapter.py``(ChannelAdapter 抽象的第一个实现)。
    状态机(F1): apply_config(enabled+凭证齐全) → installing → adapter.start
    → connected;WS 断开 → disconnected(重连退避中);鉴权失败 → error。
    """

    id = "dingtalk-bot"
    name = "DingTalk Bot"
    description = ("Chat with Ginno inside DingTalk: stream replies as AI "
                   "cards, answer agent questions with buttons, send and "
                   "preview files — your desktop Ginno stays the runtime.")
    icon = "message-circle"
    order = 30
    # 入站渠道,不向 agent 提供 tools
    tool_prefix = None

    def default_config(self) -> dict:
        return {
            "enabled": False,
            "app_key": "",
            "app_secret": "",
            "robot_code": "",
            "card_template_id": "",
            "allowed_staff_ids": [],
            "default_agent_id": "",
            "session_policy": "per_user",
            "menus": [],
            "ask_timeout_s": 600,
            "tool_policy": "confirm_highrisk",
        }

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "title": "Enabled"},
                "app_key": {"type": "string", "title": "AppKey"},
                "app_secret": {"type": "string", "title": "AppSecret",
                               "format": "password"},
                "robot_code": {
                    "type": "string", "title": "robotCode",
                    "description": "Needed for media upload; defaults to AppKey",
                },
                "card_template_id": {
                    "type": "string", "title": "AI card template id",
                    "description": ("Template published on the DingTalk card "
                                    "platform; empty = plain markdown replies "
                                    "(no typewriter streaming)"),
                },
                "allowed_staff_ids": {
                    "type": "array", "items": {"type": "string"},
                    "title": "Allowed staffIds",
                    "description": ("Empty = everyone in the app's visibility "
                                    "scope can drive the agent"),
                },
                "default_agent_id": {
                    "type": "string", "title": "Default agent for this channel",
                },
                "session_policy": {
                    "type": "string", "enum": ["per_user", "per_conversation"],
                    "title": "Session policy",
                    "description": ("per_user (P0): each staffId maps to its "
                                    "own session; per_conversation (P1 groups): "
                                    "one shared session per conversation"),
                },
                "menus": {
                    "type": "array", "title": "Menus / commands",
                    "description": ("trigger (exact or /prefix, case-"
                                    "insensitive), title, prompt template "
                                    "({date}/{time}/{staff_name}), new_session"),
                },
                "ask_timeout_s": {
                    "type": "integer", "title": "ask_user timeout (seconds)",
                    "description": "Auto-skip a parked question after this",
                },
                "tool_policy": {
                    "type": "string", "enum": ["readonly", "confirm_highrisk", "all"],
                    "title": "Tool policy",
                    "description": ("readonly=read-only tools only / "
                                    "confirm_highrisk=ask before dangerous "
                                    "tools / all=no restriction (not advised)"),
                },
            },
        }

    def install_steps(self) -> list[dict]:
        return [
            {
                "key": "intro",
                "title": "What is this",
                "body": ("Ginno can be reached from your DingTalk: you chat "
                         "with the same agent, files preview inside DingTalk, "
                         "and your desktop Ginno keeps running the turns."),
            },
            {
                "key": "create_app",
                "title": "Create an internal app on the DingTalk open platform",
                "body": ("Log in to open.dingtalk.com with your organization "
                         "account → 开发配置 → 应用开发 → 企业内部应用 → 创建应用, "
                         "then add the 机器人 capability."),
                "copy": "https://open.dingtalk.com",
            },
            {
                "key": "stream",
                "title": "Subscribe to bot messages in Stream mode",
                "body": ("In the app's 事件订阅 page choose Stream 模式推送 "
                         "(no public IP needed). Copy the AppKey and AppSecret "
                         "from 凭证与基础信息."),
            },
            {
                "key": "fill",
                "title": "Paste the credentials here and enable",
                "body": ("Fill AppKey/AppSecret above, optionally restrict "
                         "allowed staffIds, then toggle Enabled. The connector "
                         "card should show Connected within seconds."),
            },
        ]

    async def apply_config(self, cfg: dict) -> None:
        """F1 lifecycle: (re)start or stop the channel adapter on config change."""
        from ..channels.dingtalk.adapter import DingTalkChannelAdapter

        global _dingtalk_adapter
        if not cfg.get("enabled") or not (cfg.get("app_key") and cfg.get("app_secret")):
            if _dingtalk_adapter is not None:
                await _dingtalk_adapter.stop()
                _dingtalk_adapter = None
            return
        if _dingtalk_adapter is not None:
            await _dingtalk_adapter.stop()
        _dingtalk_adapter = DingTalkChannelAdapter()
        await _dingtalk_adapter.start(cfg)


_dingtalk_adapter = None  # singleton owned by DingTalkConnector.apply_config


__all__ = [
    "ChromeExtensionConnector",
    "BrowserProfileConnector",
    "DingTalkConnector",
    "WeComConnector",
    "VibeIslandConnector",
    "ensure_builtin_connectors",
    "STATUS_CONNECTED",
    "STATUS_DISCONNECTED",
    "STATUS_NOT_INSTALLED",
]
