---
name: browse
description: 用内嵌浏览器打开登录态网站、点按钮、过验证码。Use when the user asks to open a site, click through a SPA, log in, or scrape a page that needs a real browser.
trigger: both
tools: [browser_eval, browser_snapshot, browser_handoff, browser_screenshot]
---

# Browse（内嵌浏览器）

Ginno 右侧分栏就是真浏览器。登录态跟用户共享。本轮你已经有 `browser_*`。

**禁止** `web_fetch`、`mcp_playwright_*`、自己编 `@N`、handoff 后再开一个新 Space。

## 打开网站（必须按这个顺序，一次 `browser_eval` 写完）

```js
await useOrCreateTaskSpace('sf ai site')
const tab = await openOrReuseTab('https://ai.sf-express.com', { wait: true, timeout: 20 })
cliLog(tab)
const snap = await snapshotText()
cliLog(snap)
```

（M0 方言没有 `if` / `&&` / 箭头函数。判断写在下一轮中文里，或看 `tab.error` / `tab.login_wall` 再单独 `handOffTaskSpace`。）

然后根据 **这一次** snapshot 里的 `[ref=N]` 再点。没有 snapshot 就没有 `@N`。

用户只说了主机名（`ai.sf-express.com` / `x.com`）时，写成 `https://主机名`。`openOrReuseTab` 也会自动补 `https://`。

## 看完 snapshot 再决定

- 页面正常 → 用这次 snapshot 的 `@N` 点 / 填 / 读文字，用中文回答用户看到了什么。
- URL 或标题像登录 / SSO / CAS / captcha / 验证码 / 支付 → **立刻** `handOffTaskSpace('需要你登录 <站点>')`，不要猜密码，不要再点。
- `tab.url` 还是 `about:blank`，或 `tab.error` / `ensureRealTab()` 说 blank → 不要编页面内容。告诉用户：「标签还是空白，请完全退出 Ginno 后重开，或在分栏点重试」。可以再 `openOrReuseTab` 一次，仍空白就停。
- `click` 回报 unknown ref → 再 `snapshotText()`，只用新编号。

## 硬规矩

1. **一个任务一个 Space 名**（3–6 个英文词，稳定，例如 `sf-ai-site`）。同一站点接着看，必须复用这个名字。
2. 人点了「交还」或 `browser_resume` 之后，先 `takeOverTaskSpace('同一个名字')`，**禁止** `useOrCreateTaskSpace('另一个名字')`。
3. 登录墙 / 验证码 / 支付 / 扫码 → `handOffTaskSpace(reason)`。转会暂停，人在右侧画面上操作，点「交还」你再继续。
4. `completeTaskSpace({ keep })` **单独一轮**，不要和工作脚本写在一起。
5. `@N` 只对**最近一次** `snapshotText()` 有效。
6. 引擎是 fake / 没有真标签时，直说「真 Chrome 没起来，请完全退出后重开」，不要假装打开了。

## 交还后接着干

```js
await takeOverTaskSpace('sf ai site')
cliLog(await snapshotText())
```

## 工具

- `browser_eval(code, space?)` — 一次 JS 写完整段，helpers 已预注入
- `browser_snapshot(space?)` — 只要观察
- `browser_handoff(space?, reason?)` — 显式交给人
- `browser_screenshot(space?)` — 截图进 Artifacts
