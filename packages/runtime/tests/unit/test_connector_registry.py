

# ---- report_meta 广播守卫（2026-11-18 连接器页请求风暴修复钉子）------------
# 缺陷：report_meta 无条件 _notify——GET /api/connectors 每次都经
# ensure_builtin_connectors → ensure_a2a_hub → report_inbound_meta 上报
# 派生 extra，内容一字未变也广播 connector_status_changed → 前端 WS 收
# 到即 refresh → 再 GET……自激环实测 6s 652 次请求（playwright 实证），
# 打满浏览器 6 连接池 = 连接器页永久「正在加载」、toggle 卡死、webview
# 最终崩溃。守卫语义：meta 无实质变化不广播（与 report 同状态早退对称）。
def test_report_meta_no_change_no_broadcast():
    from ginno_runtime.connectors.registry import registry
    from ginno_runtime.connectors.builtin import ensure_builtin_connectors

    reg = registry()
    ensure_builtin_connectors()
    calls: list[str] = []
    reg._listeners.append(lambda ev, snap: calls.append(str(snap.get("id"))))

    calls.clear()
    reg.report_meta("a2a", extra={"any": "value"})
    first = len(calls)  # 首次上报（状态从无到有/extra 变化）允许广播
    reg.report_meta("a2a", extra={"any": "value"})  # 完全相同 → 不得广播
    assert len(calls) == first, f"相同 extra 重复上报不得广播：{calls}"
    reg.report_meta("a2a", extra={"any": "other"})  # 实质变化 → 广播
    assert len(calls) == first + 1


def test_ensure_builtin_connectors_idempotent_no_broadcast():
    """GET /api/connectors 的每次调用都会跑 ensure——第二次起零广播。"""
    from ginno_runtime.connectors.registry import registry
    from ginno_runtime.connectors.builtin import ensure_builtin_connectors

    reg = registry()
    ensure_builtin_connectors()
    calls: list[str] = []
    reg._listeners.append(lambda ev, snap: calls.append(str(snap.get("id"))))
    calls.clear()
    ensure_builtin_connectors()
    assert calls == [], f"幂等 ensure 不得产生广播（风暴源）：{calls}"
