# @ginno/mod-runner

Ginno Claude Code Mods 兼容层的 **per-mod Node runner**:每个 mod 一个进程,装载其清单与
hooks 模块、执行 `register(on, options)`、把 `$` 调用与 `next()` 整形为帧发给 broker,
并把 hook 的答案帧化回传。零 npm 运行时依赖(esbuild 单文件);mod 本体的 `.ts` 靠
Node ≥ 22.18 原生 type stripping 直接加载。

runner 零策略:链序、matcher 预筛、预算、grants、`next` 单次性的**执行**全在 Rust
broker(见 `docs/claude-code-mods-design.md` §3);runner 只保留必须活在 mod 闭包旁边的
东西(press/timer 回调表、防双发的本地 next 缓存、Wall-clock 兜底)。

```
broker (Rust) ──stdio 换行 JSON──> runner --mod <dir>
                                    ├─ loader.ts      plugin.json + hooks.json(modules[0]) → import ESM
                                    ├─ module.ts      register(on, options) + createOn 校验
                                    ├─ api.ts         $ shim(served 命名空间 + 未实现成员命名 reject)
                                    ├─ elements.ts    Box/Text/Button 构造器、树校验与序列化
                                    ├─ hook-runtime.ts 事件执行、next/catch、press/timer 回调表
                                    └─ index.ts       帧循环、5s ping、EOF/shutdown 退出
```

## 帧协议(与 broker 对齐的契约点)

词汇表:`call / result / event / next / catch-call / notify / hello`。
请求按 `id` 关联;`invocation` 贯穿一次 hook 派发。规范源:broker 侧
`crates/mod-broker/src/protocol.rs` 模块注释。

- runner 启动:`--mod <dir>` → 读清单、import 模块 → **call
  `runner.loaded{args:{name,version,userConfig}}`** → broker 回
  `{config:{…}}`(未实现此 call 的 broker 超时 2s,runner 用 userConfig 默认值继续)→
  合并 options 跑 `register` → **call `runner.hooks-registered{args:{hooks}}`** →
  broker 回 result(value `"start"`)。matcher 序列化:string / array 原样,
  `RegExp → {"regexp":source,"flags":flags}`。
- `call` 帧:`{v,id,kind,ns,method,args,mod,session?,invocation?}`——`mod` 恒带,
  `session`/`invocation` 在 hook 上下文里回显(broker 据此暂停该 hook 的 busy 预算;
  唯一例外 `clock.sleep` 不暂停)。`next` 帧:`{kind:'next',invocation,e}`,catch 里的
  next 额外带 `phase:"catch"`。
- **hook 的答案 = event 帧的 result**:`result{id, ok:true, value}`(对象或 `null`
  都是合法答案)或 `ok:false`。失败 code:`hook-throw`(含 next 协议错误**原样透传**
  的 code——broker 靠 code+message 判定「下方失败是事件自身的失败」)、
  `no-result`(返回 undefined/非对象)、`no-hook`。
- **band 树**:Button 的闭包留在 runner,序列化树带 **runner 铸造的
  `actionId:"<mod>:a<N>"`**(按绘制内位置编号——同一棵树字节一致,broker 才能判
  「树未变、保持 generation」)。press 由 broker 回发
  `call{ns:"ui",method:"press",args:{actionId,generation}}`,runner 执行 onPress 后
  `result` 回执(过期 generation 由 broker 侧丢弃)。
- **timer**:`$.clock.after/every` 帧化为 `call clock.after/every`(结果 `{timer:"t<N>"}`;
  集合已关闭时 `{timer:""}` = 死句柄);broker 到点发
  `call{ns:"clock",method:"fire",args:{timer}}`,runner 跑完闭包(含 await)才
  `result` 回执——broker 以此实现 close 语义的「await 正在跑的回调」;取消走
  `call clock.cancel{timer}`。本地不留 timer。
- **catch**:hook 失败后 broker 发 `catch-call{invocation,event,payload,
  failure:{kind,message},deadlineMs}`;`failure` 成为 catch 里 `next.error`。catch 的
  答案**回在 catch-call 帧的 id 上**(不是 event 的 id):对象 = 接管答案,
  `ok:false code:"no-catch"` = 无 handler(broker 静默跳过,它不知道哪条注册挂了
  catch)。
- **hook-timeout**:预算到点 broker 发 `notify{method:"hook-timeout",args:{invocation}}`
  → runner 放弃该 hook(迟到的 `next` 拿 undefined,迟到答案丢弃)。
- **心跳**:runner 每 5s 发 `notify ping`;broker 15s 无 ping 判死。stdin EOF 或
  `shutdown` → 进程退出。
- 未实现的 `$` 命名空间(`$.settings`/`$.telemetry`/`$.audio`…):调用即命名 reject
  (`no implementation for <ns>.<method>`),`$` 只读;`ui.log/toast/status` fire-and-forget。
- **兼容注意**:源码无 TS 参数属性(`constructor(private …)`),dev 模式可直接
  `node src/index.ts --mod <dir>`(type stripping 不支持参数属性)。

## 构建 / 测试

```bash
pnpm --filter @ginno/mod-runner build      # esbuild → dist/mod-runner.mjs(单文件零依赖,署名头经 @license 保留)
pnpm --filter @ginno/mod-runner test       # vitest:58 用例(协议级 chain / matcher / elements / module / api / loader / clock)
pnpm --filter @ginno/mod-runner typecheck  # tsc --noEmit
```

`examples/token-weather/` 是官方示例的 vendored 副本(MIT,Anthropic 博客原文,文件头保留
出处),也是 e2e 验收 mod:band 随 turn 重绘、state 记账、`ui.render{component=AbovePrompt}`。

## 出处

`api.ts` / `elements.ts` / `module.ts` / `values.ts` / `matcher.ts`(表与序列化)/
`clock.ts` 移植自 deepseek-harness `packages/experimental/claude-code-mods`(MIT,
Copyright (c) 2026 DeepSeek),按本仓库设计改为发帧实现;链语义(chain/engine/matcher
评估/surfaces/host-ops)**不**移植,由 Rust broker 按 `docs/claude-code-mods-design.md`
§15 规格实现。
