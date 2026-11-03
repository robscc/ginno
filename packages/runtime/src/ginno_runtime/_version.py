__version__ = "0.1.0"


def ginno_user_agent() -> str:
    """统一的对外 User-Agent（网关按它识别 Ginno 流量）。

    所有出网请求——build_model 三协议、settings 页 ping/verify 探测、
    模型列表拉取、classifier、ASR——一律带这个值，单一来源。
    """
    return f"ginno/{__version__}"
