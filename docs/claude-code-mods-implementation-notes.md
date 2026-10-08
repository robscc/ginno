# Claude Code Mods 兼容层实施记录(P0)

- 日期:2026-10-07
- 分支:`feat/claude-code-mods`
- 设计:`claude-code-mods-design.md`(v2,Rust broker 分布式编排)
- 状态:P0 落地 + 三 case e2e 通过;未做 `make app` 桌面集成(Tauri 嵌入 broker 留 P1)

## 交付清单

| 组件 | 位置 | 测试 |
|---|---|---|
| Rust broker(链编排/op 表/SurfaceTable/runner 监督/grants/socket+CLI) | `crates/mod-broker/` | cargo 45(26 单测 + 19 集成,DSH chain/matcher spec 移植) |
| Node runner(装载/$ shim/元素/createOn,移植 DSH MIT) | `packages/mod-runner/` | vitest 58 + esbuild 单文件 dist/mod-runner.mjs |
| Python 侧(ModChannel/五事件抽点/Python 后端 op/classic 桥接/API/settings) | `ginno_runtime/mods/` + graph.py 等五处插点 | pytest 33 |
| 前端(band 插槽/元素渲染器/ModToastHost/Mods 设置页/引擎事件分支) | `apps/web/src/` | tsc --noEmit |

## e2e 结果(脚本在会话 tmp,未入库)

1. **Token Weather(官方示例)** ✅:`☀ Clear 18% of context 36.1k/200k` band 渲染,generation 随 turn 递增;dev-spawn 与显式 socket 两条路径都通。
2. **claude-music(kennethleungty,classic hooks)** ✅:HookDispatcher 注册其 3 个 shell hook,SessionStart 真实执行(`${CLAUDE_PLUGIN_ROOT}` 注入,副作用文件落盘)。
3. **ginno-tamagotchi(自写示例,`packages/mod-runner/examples/ginno-tamagotchi`)** ✅:band 宠物 + **Button press 全回路**(Python notify → broker 代际校验 → runner onPress → session.usage 回 Python → state.set → 订阅重绘 → 新 generation)。

## 集成阶段修的接缝(并行开发的典型对齐问题)

1. **broker 补 `runner.loaded` → `{config}` 分支**:userConfig 推送链路原来断裂(runner 等不到 config 用 2s 超时兜底)。
2. **Python `build_config()` 对齐 broker `parse_config` schema**:mods.items 需要 `dir`(从 scan_installed_mods 补)、nodePath/runnerPath 必须是已解析路径(新增 `resolve_runner()`)、budgets 键名对齐。
3. **channel `_connect_loop` 丢失 token**:`_resolve_endpoint` 解析出的 token 从未存入 `self._token`,默认路径 hello 恒发 null → broker 拒绝且不回帧 → 表现为 TimeoutError(显式传参路径掩盖了此 bug)。
4. **ensure_broker 的 token 竞态**:socket 文件先于 token 文件出现,等待条件改为两者齐备。
5. **`_absorb_ready` 把 hooks 描述字符串当列表**:逐字符遍历全被跳过 → 注册事件集合恒空 → 事件在 Python 侧短路回显(最隐蔽的一个,表现为"链路通但无任何 mod 活动迹象")。
6. **scan 区分 shape**:classic 形态(`{"hooks":{...}}`)误入 broker 会 crash-loop(没有 JS 模块可加载);现在 classic 只进 dispatcher,js("modules")才进 broker。hello 超时 5s→30s(首次握手要并行拉 runner,broker 侧 HANDSHAKE_TIMEOUT 20s)。
7. **press/timer 回调的 session 上下文**:hook 已 settle,回调期间的 `$` 调用没带 session → Python 后端 op 拒绝;runner 增加 ambientSession(press/clock.fire 帧的 session 在回调期间生效)。
8. **`session.usage` 零形状兜底**:未知会话返回 `{context:{tokens:0,window:0}}` 而非 None(mods 规范 usage 恒为对象,mod 会解构 `.context`)。
9. **`session.usage` 主路径形状违规(2026-10-08)**:有用量时返回扁平 totals(无 `context` 键),reading 类 mod(token-weather)解构 `.context` 落空 → 永不渲染。已改为规范形状:`tokens` 取最近一次调用的 whole-prompt input(cache 计入),`window` 取新设置 `mods.contextWindow`(默认 200k,runtime 不跟踪模型窗口)。

## e2e 发现的安全问题(重要)

**claude-music 的 SessionStart hook 会自动 `brew install mpv`**(缺 mpv 时),并改写 `~/.claude/settings.json` 注入 statusLine。classic hooks = 任意 shell,当前无任何 grants 管控——这与 JS mods 的 IPC 边界管控形成反差。

**Follow-up(P1)**:
- classic hooks 纳入 grants(命令白名单/首次运行确认),至少对"包管理器调用"和"settings 写入"设默认拒。
- `HookResult.inject`(additionalContext)接进 state(runtime 侧已记 P1)。
- Makefile 加 `mod-broker`/`mod-runner` target;Tauri 嵌入 broker(桌面形态)。
- 前端 `mod.ui.press` WS 帧 → channel 的转发接线(引擎分支已留)。

## 已知限制

- band 是"整链一棵树"(最外层 hook 的 ui.render 答案),per-mod band 排列 P2。
- matcher 的 regexp 由 Rust regex 评估,与 JS 方言差异未复核(validate 报告注明)。
- per-session 的 fs 根(workspace 全局限一根)P1 再按会话细化。
