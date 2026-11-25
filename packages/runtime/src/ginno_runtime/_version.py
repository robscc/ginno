# ⚠️ 发版必须 bump 这里（第四处）。
# 历史上本文件长期停在 0.1.0，而 pyproject/tauri.conf/package.json 已到 0.3.3——
# 发版 runbook 只列了那三处，漏了本文件，导致：
#   · `ginno --version` 报 0.1.0
#   · /api/health 的 version 报 0.1.0（build.fingerprint 却是对的）
#   · A2A Agent Card 的 version 报 0.1.0（远端对端看到错版本）
#   · ginno_user_agent() 返回 ginno/0.1.0 —— 而网关按 UA 识别 Ginno 流量
# 现改为**单一真源**：runtime 内所有版本展示一律 import 本模块，
# 并有 tests/unit/test_version_single_source.py 在 CI 钉住它与 pyproject 一致。
__version__ = "0.5.2"


def ginno_user_agent() -> str:
    """统一的对外 User-Agent（网关按它识别 Ginno 流量）。

    所有出网请求——build_model 三协议、settings 页 ping/verify 探测、
    模型列表拉取、classifier、ASR——一律带这个值，单一来源。
    """
    return f"ginno/{__version__}"
