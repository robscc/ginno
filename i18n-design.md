# i18n-design.md — Ginno 国际化设计（zh-CN / en）

> v2 定稿 · 2026-10-04。已确认四项决策：
> 1. `language` 与 `prompt_language` **合并为单一设置** `language`
> 2. 模型回复语言**跟随 `language`**
> 3. 事件契约 key+params **一次性全切**
> 4. 前端采用 **next-intl**

## 1. 总览

单一语言决策点：前端 settings 解析出 effective locale（`en` / `zh-CN`）后随请求下发给
runtime；web / runtime / Rust / 扩展都不做各自的 locale 猜测，避免「UI 一语言、通知另一语言」漂移。

```
settings.language ──解析(auto)──> en | zh-CN
   │  驱动: web UI (next-intl) · runtime 文案 (t()/_()) · prompt 选边 · 模型回复语言
   └─ 下发: HTTP/WS 头 X-Ginno-Language → contextvar → turn 内全程生效
```

## 2. 语言模型与设置迁移

settings.json：

- 新增 `language: "auto" | "en" | "zh-CN"`，默认 `auto`
- **移除** `prompt_language`（并入 `language`）

一次性迁移（settings 加载时执行并回写）：

```python
language = s.get("language") or (
    "zh-CN" if s.get("prompt_language") == "zh" else
    "en"    if s.get("prompt_language") == "en" else
    "auto"
)
s.pop("prompt_language", None)
```

- 迁移保真：旧 `prompt_language=zh` → `zh-CN`；`=en` → `en`（行为不变）；全新安装 → `auto`
- `auto` 仅在前端解析：`navigator.language` 以 `zh` 开头 → `zh-CN`，否则 `en`；
  不支持的系统语言（如 `ja`）静默落 en，不打扰用户
- 涉及改动：`paths.py`（settings 校验/默认值）、`lang.py`、`world_state.py`、
  web `GeneralSettings.tsx`（单一语言下拉替代 prompt_language）、删除 `apps/web/src/lib/promptLang.ts`

## 3. 数据契约：事件/错误一次性全切 key + params

runtime → web 的**用户可见**事件文案全部改为：

```json
{ "i18n_key": "errors.session_not_found", "params": { "id": "abc" } }
```

- **一次性全切**：单个 PR 内完成所有发射点（枚举方法见附录 A），不分批、不留双轨
- 前端渲染兼容：`i18n_key ? t(...) : 原样直显`——历史会话（成品字符串）重放不坏
- `toolLabels.ts` 并入 `messages` 的 `tool.*` 域，与 runtime `tool_labels` 设置既有迁移路径不变
- **不翻译清单**（永远英文，检查脚本显式排除）：fork 标记、split 关键字、协议保留字、日志
- **日志一律英文**（可 grep 优先）；`t()`/`_()` 仅用于面向用户的输出

## 4. 前端：next-intl（无 i18n 路由模式）

采用 next-intl 的 **without i18n routing** 用法——Ginno 是静态导出（`output: "export"`）
+ 语言由 settings 决定（非 URL），不需要也不应该用 locale 路由 / middleware。

```
apps/web/messages/en.json          # source 语言（权威）
apps/web/messages/zh-CN.json
apps/web/src/i18n/
  config.ts       # locales、defaultLocale='en'、auto 解析
  provider.tsx    # client：NextIntlClientProvider + setLocale
  global.d.ts     # AppConfig 类型增强（key 全类型检查）
```

要点：

1. **Provider**：client 组件包住应用；`locale` 来自 settings——首屏同步读
   `localStorage["ginno.language"]` 镜像避免闪烁，settings 加载后校正；
   messages 直接 `import` → 必然进静态导出产物，**无漏打包风险**
2. **切换**：`setLocale()` → 写 settings API + localStorage + context → 全树重渲染，不刷新页面
3. **类型安全**：`declare module 'next-intl' { interface AppConfig { Locale: 'en'|'zh-CN';
   Messages: typeof import('../../messages/en.json') } }` → `useTranslations` 的
   namespace/key 拼错**编译失败**（en 为 source）
4. **静态导出注意**：构建期预渲染（SSG）时 locale 固定为 en，因此所有用
   `useTranslations` 的组件必须是 client 组件——Ginno 前端本就近乎全 client，
   新增 server 组件时受此约束
5. **格式化**：日期/数字用 `useFormatter()`（原生 Intl）；复数/插值用内建 ICU 语法
   `t('msg', {count})`
6. key 规范 `<domain>.<name>`：`chat / settings / connectors / kb / code / right /
   errors / notify / tool / ext`

## 5. Runtime

`lang.py` 升级为 i18n 核心，双轨制：

| 轨道 | 适用 | 说明 |
|---|---|---|
| 内联 `t(en, zh)`（既有 227 处，保留） | prompt 模板等大段文本——文案即代码 | `t()` 改读 contextvar locale（不再绑 prompt_language） |
| key catalog（新增） | 错误码/状态/通知标题等结构性小文案 | `ginno_runtime/i18n/{en,zh_CN}.json` + `_("errors.x", id=..)` |

- locale 传递：FastAPI 依赖读 `X-Ginno-Language` 头 → `contextvar`；WS 连接建立时带上，turn 全程生效
- 无请求上下文时（scheduler / 后台任务）：`i18n.current()` 回落直读 settings
- **通知文案**：runtime 用 `_()` 生成成品字符串传给 Rust 显示（Rust 不持 catalog）
- fallback：请求 locale → en → key 原样；未命中记 `WARNING i18n_missing key=...`

## 6. Prompt 层

- 大段模板维持模块级双语常量 + `t()` 选边（统一读 `language`）
- system prompt 注入回复语言指令块：`lang.py::response_lang_directive()` →
  "Always respond in Simplified Chinese / English..."（跟随 `language`；合并后
  UI 与回复语言一致，不再有组合分歧）
- compaction / microcompact 重注入 lead-in 文案（cce43fa 的折叠短文案）双语化：走 `_()`
- 术语表按语言选边；历史消息不回翻，新 turn 生效

## 7. Tauri/Rust

原则：**Rust 不持文案**。对话框/菜单文案由前端调用时作为参数传入；通知由 runtime
传成品字符串。`lib.rs` ~49 处字符串中真正用户可见的逐个清理（预计 <10 处）。

## 8. 扩展

`build.py` 从 `apps/web/messages/` 提取 `ext.*` 子树，生成 Chrome 标准
`_locales/{en,zh_CN}/messages.json`（Chrome 按浏览器语言自动选择）+ 关键文案内嵌兜底。
维持「单一来源、物化生成」架构。

## 9. 缺失翻译：运行时提示

- fallback 链：`请求 locale → en → key 原样`
- 未命中：web console warn / runtime `WARNING i18n_missing`（sidecar.log）
- dev 调试（`?i18n=debug`）：未命中 key 的 UI 红色显示原始 key

## 10. 缺失翻译：构建期检查（`make check`，`make app` 前置）

1. **TS 类型**：next-intl `AppConfig` 增强后 tsc 即查（en source 的 key 拼写）
2. **catalog diff**（`scripts/check_i18n.py`）：zh-CN vs en 的 key 集合双向 diff
   （缺/多都报错）+ ICU 占位符一致性（`{name}` 同名同数量）；覆盖
   `apps/web/messages/` 与 `packages/runtime/src/ginno_runtime/i18n/`
3. **eslint**：自写规则禁止 JSX 裸文案（带白名单），P1 起 warn、P3 起 error
4. **t() AST 扫描**：两参数非空、无局部变量遮蔽 `t`（已知坑）、无 f-string 拼接协议保留字
5. **PyInstaller**：`i18n/` 目录加入 `ginno-runtime.spec` 的 datas；启动时
   `i18n_health_check()` 验证两 catalog 可加载且 key 集合一致，失败 →
   `ERROR i18n_bundle ...` 指明缺哪个文件——启动即暴露，对照 zlib 教训：
   bundle 问题不能等用到时才炸

## 11. 实施计划

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| P0 地基 | 合并设置+迁移；next-intl 安装/Provider/类型；runtime contextvar + `_()` + spec datas + health check；`make check` | settings 页双语；坏 key 编译失败 |
| P1 Web 全量 | 86 文件迁移 `useTranslations`；toolLabels 并入；单一语言下拉；eslint warn | 全 UI 中英即时切换 |
| P2 契约全切 | 事件/错误 key+params **一次性全切**（附录 A）+ 渲染兼容；回复语言指令；通知双语；compaction lead-in | 两语言各跑一轮完整 turn 冒烟 |
| P3 收尾 | 扩展生成 catalog；eslint error；复数/日期打磨；README | `make app` 全量验证 |

每阶段 `make app` 后**完全退出重启** Ginno 验证。

## 12. 风险

- **SSG 预渲染**：server 组件用 `useTranslations` 会在构建期固化为 en——约束见 §4.4
- **一次性全切回归面大**：附录 A checklist + 双语言冒烟脚本对冲
- **86 文件迁移**：机械但量大，eslint warn 兜底防漏

## 附录 A：事件发射点枚举方法（P2 执行）

```bash
# 所有向 WS 流发送用户可见文本的位置建 checklist：
rg -n 'send_json|yield.*event|"text":' packages/runtime/src/ginno_runtime/api/stream.py
# 逐项登记：事件类型 → i18n_key → params → 前端渲染点 → 双语言验证 ✓
# 进 implementation-notes 跟踪，全切 PR 附完整对照表
```
