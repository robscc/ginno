"""bash 工具环境变量注入（2026-11-09，PR #61 后续特性）。

用户的本机常驻代理（ClashX 7890 等）不会被 GUI 启动的 sidecar 继承，
导致 git/gh/curl 访问外网超时——每次都要在命令里手写
``git -c http.proxy=…`` 或 ``export HTTPS_PROXY=…``。

本模块让用户在 ``~/.ginno/settings.json`` 里声明一份环境变量：

    { "env": { "HTTP_PROXY": "http://127.0.0.1:7890",
               "HTTPS_PROXY": "http://127.0.0.1:7890" } }

bash 工具（以及经由 bash 执行的 skill 命令）起子进程时将其合并进
``os.environ``。纪律：

- 每次调用现读文件（<1ms），改 settings 立即生效，无需重启；
- 值必须是字符串/数字/布尔，非字典或坏 JSON 一律静默降级为不注入；
- 前缀键（如 ``PATH_``）不特殊处理；``PATH`` 允许覆盖——注入值拼接在
  原值**之后**（``extra:PATH`` 语义），避免破坏登录 shell 的 PATH 链；
- 任何异常都不允许阻塞 bash 工具——最坏情况就是「一切照旧」。
"""

from __future__ import annotations

import json
import logging
import os

from ..paths import settings_path

log = logging.getLogger(__name__)


def load_extra_env() -> dict[str, str]:
    """读 settings.json 的 ``env`` 字典；缺失/坏数据返回空 dict。"""
    try:
        p = settings_path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        env = data.get("env")
        if not isinstance(env, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in env.items():
            if not isinstance(k, str) or not k:
                continue
            if isinstance(v, bool):
                out[k] = "true" if v else "false"
            elif isinstance(v, (int, float)):
                out[k] = str(v)
            elif isinstance(v, str):
                out[k] = v
        return out
    except Exception:  # noqa: BLE001 — 注入失败绝不能挡住 bash 工具
        log.warning("env_inject: settings.json 读取失败，跳过环境注入", exc_info=True)
        return {}


def build_child_env() -> dict[str, str] | None:
    """构造 bash 子进程 env；无需注入时返回 None（= 继承父进程 env）。"""
    extra = load_extra_env()
    if not extra:
        return None
    env = dict(os.environ)
    for k, v in extra.items():
        if k == "PATH":
            env[k] = f"{env.get(k, os.defpath)}:{v}"
        else:
            env[k] = v
    return env
