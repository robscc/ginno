"""Browser companion module (browser-companion-extension-design.md).

Two tracks behind one tool surface (:mod:`..tools.browser_tools`):
- B 轨 dedicated-profile Chrome over CDP — :mod:`.executor` ``ProfileBackend``
- 扩展轨 the user's real Chrome via the companion extension — :mod:`.relay`
"""

from .config import BrowserConfig, load_browser_config
from .executor import ProfileBackend, find_chrome, get_profile_backend

__all__ = [
    "BrowserConfig",
    "load_browser_config",
    "ProfileBackend",
    "get_profile_backend",
    "find_chrome",
]
