# 浏览器扩展轨 — 需真机（真实 Chrome）验证清单

> 背景：issue #19 / #20。扩展轨的纯逻辑（relay 帧解析、响应路由、断连清理、
> FIFO 队列、分块重排）已在 `packages/runtime/tests/unit/test_browser_connectors.py`
> 覆盖（fake WebSocket 驱动，本地可跑）。以下各项依赖真实 Chrome 的扩展宿主
> 行为，**无法本地验证**，发版前须真机过一遍。

## A. manifest 权限（issue #20）

- [ ] `manifest.json` 已补 `"nativeMessaging"` 权限（2026-11-05 加入）。
      `background.js` 的 `chrome.runtime.connectNative("com.ginno.browser_host")`
      （nativeDiscoverPort 探测 + nativeTransport）在此权限缺失时会直接抛
      异常走 fallback——**旧行为是静默 fallback，补权限后须确认 WS 主路径未回归**。
- [ ] 加载扩展（chrome://extensions → 加载已解压的 `~/.ginno/browser-extension/`）
      后 chrome://extensions 无权限告警；service worker 控制台无
      `Cannot call connectNative()` 类报错。
- [ ] 停掉 sidecar（native host 桥不通）时，扩展回退 WebSocket 直连
      `ws://127.0.0.1:<port>/extension` 成功（savedPort 路径）。

## B. native messaging 端到端

- [ ] native host（`com.ginno.browser_host`）注册表/manifest 路径指向当前安装，
      Chrome 能拉起 host 进程。
- [ ] `relayConnect` 握手成功；`relayData` / `relayDataChunk`（256KB 分块）双向
      大消息（截图 base64 >256KB）不丢块、顺序正确。
- [ ] native host 崩溃/退出后，扩展自动回退 WS 或按退避重试，UI 无假死。

## C. WS relay 端到端（需要 sidecar + 真扩展）

- [ ] `/extension` 连入后连接器页状态 `Connected · Chrome`，version/capabilities
      与 manifest 一致。
- [ ] `ping`/`pong` 心跳 20s 周期正常，长空闲（>5min）不掉线。
- [ ] 双开（第二个 Chrome profile 连入）时旧连接被 `close()` 顶替，工具调用
      落到新连接（RelayState 单连接语义）。
- [ ] 工具调用 60s（screenshot 90s）超时路径：拔掉 Chrome 网络代理让页卡死，
      确认 `QUEUE_TIMEOUT` 文案回到对话。
- [ ] 页内 Stop 按钮 → `stopToolExecution` 中断在途调用，后续调用可正常发起。

## D. 断连恢复

- [ ] Chrome 重启（service worker 重启）后扩展自动重连，`configChanged`
      tab scope 在重连后不丢。
- [ ] sidecar 重启期间扩展反复重连，恢复后 tab 归属判定（tabGroups 分组
      scope）仍正确。
- [ ] 断连瞬间的在途调用收到「扩展连接已断开」错误（对应单测
      `test_relay_disconnect_fails_pending_and_resets_state` 的真机形态）。

## E. 鉴权

- [ ] 当前 `/extension` WS **无 token 鉴权**，仅靠 127.0.0.1 本机绑定防护
      （`host_permissions` 亦仅本地）。如需暴露到非本机（远程调试/容器），
      必须先补握手 token——列为后续 hardening 项，不在本轮改动范围。
