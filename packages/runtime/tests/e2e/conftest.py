"""e2e 会话级兜底：收割本测试会话泄漏的 dist sidecar。

测试内的 ``finally: stop_sidecar(...)`` 是第一道防线（见 _sidecar.py）；
这里兜第二道——测试函数崩溃/超时/pytest 中断时 finally 可能不执行。
做法是快照 diff：会话开始前记一份 dist/ginno-runtime 的 pid，会话结束
时只杀**新出现**的 pid。用户自己跑着的 sidecar（桌面 app / ginno exec）
在快照里已存在，永远不会被碰。
"""

from __future__ import annotations

import _sidecar as sc
import pytest


@pytest.fixture(scope="session", autouse=True)
def harvest_leaked_sidecars():
    before = set(sc.dist_sidecar_pids())
    yield
    leaked = [pid for pid in sc.dist_sidecar_pids() if pid not in before]
    if leaked:
        sc.harvest(leaked)
