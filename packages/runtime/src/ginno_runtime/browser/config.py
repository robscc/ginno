"""The `browser` settings block (browser-companion-extension-design.md §6).

Two execution tracks share this config:
- 扩展轨 (chrome-extension connector): drives the user's real Chrome via
  the companion extension over the relay endpoint.
- B 轨 fallback (browser-profile connector): Ginno launches a dedicated
  real Chrome with ``--user-data-dir=~/.ginno/browser-profile`` and talks
  CDP directly — no extension needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths

# Domains where agent-driven browser actions need explicit confirmation
# (payment / mail / cloud consoles). Action tools check the active tab's
# host against this list; browser_navigate seeds matching `ask` rules.
DEFAULT_SENSITIVE_DOMAINS: tuple[str, ...] = (
    # payments
    "pay.weixin.qq.com",
    "open.weixin.qq.com",
    "alipay.com",
    "bill.tenpay.com",
    "paypal.com",
    "stripe.com",
    # mail
    "mail.google.com",
    "outlook.live.com",
    "mail.qq.com",
    "mail.163.com",
    # cloud consoles
    "console.aws.amazon.com",
    "portal.azure.com",
    "cloud.tencent.com",
    "console.cloud.google.com",
)


@dataclass
class BrowserConfig:
    enabled: bool = True
    # 未装扩展时 B 轨的启用方式: ask (提示一次) / auto (静默降级) / off
    fallback_profile_mode: str = "ask"
    sensitive_domains: list[str] = field(
        default_factory=lambda: list(DEFAULT_SENSITIVE_DOMAINS)
    )
    # agent 开新标签时是否把 Chrome 窗口带前置焦点
    auto_open_group: bool = True
    # Chrome 可执行文件覆盖(空 = 自动发现)
    chrome_path: str = ""
    # B 轨是否无头(默认 False: 浏览器操作默认可见、可接管)
    headless: bool = False
    # 截图等待页面加载的超时(秒)
    load_timeout_s: int = 10
    # relay 端口(0 = 随机;固定值便于扩展配置迁移)
    relay_port: int = 0

    def sensitive_match(self, url: str) -> str | None:
        """Return the matching sensitive domain for ``url``, if any."""
        if not url:
            return None
        host = url.lower()
        # crude host extraction when a full URL is passed
        if "://" in host:
            try:
                host = host.split("://", 1)[1].split("/", 1)[0]
            except IndexError:
                return None
        for d in self.sensitive_domains or []:
            d = d.lower().strip()
            if d and (host == d or host.endswith("." + d) or d in host):
                return d
        return None


def load_browser_config(settings: dict[str, Any] | None = None) -> BrowserConfig:
    if settings is None:
        p = paths.settings_path()
        try:
            settings = json.loads(p.read_text() or "{}") if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            settings = {}
    stored = settings.get("browser", {}) or {}
    known = (
        "enabled",
        "fallback_profile_mode",
        "sensitive_domains",
        "auto_open_group",
        "chrome_path",
        "headless",
        "load_timeout_s",
        "relay_port",
    )
    kwargs = {k: stored[k] for k in known if k in stored}
    cfg = BrowserConfig(**kwargs)
    if cfg.fallback_profile_mode not in ("ask", "auto", "off"):
        cfg.fallback_profile_mode = "ask"
    return cfg


def profile_dir() -> Path:
    """B 轨 Chrome 的专用 user-data-dir(用户可写,首次启动自动创建)。"""
    p = paths.home() / "browser-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def extension_dir() -> Path:
    """物化的扩展目录(~/\.ginno/browser-extension)——用户在 chrome://extensions
    里加载的就是这个文件夹(分发模式见设计文档 §7.3)。"""
    return paths.home() / "browser-extension"
