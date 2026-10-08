---
name: browser
description: 操作用户的 Chrome 浏览器（打开/切换标签页、点击、输入、截图、读页面、跑 JS、抓网络请求）。Use when the user asks to browse/open/scrape/automate a web page.
trigger: both
sticky: true
tools: [browser_*]
---

# 浏览器操作 (Browser automation)

你现在可以操作用户的 Chrome 浏览器。双轨：优先走用户的日常 Chrome（companion
扩展经 WS relay 连接，沿用其登录态与扩展）；扩展未连接时自动回落到专用
profile Chrome（首次使用时按需启动）。

## 工作流

1. **先拿 tab 清单**：`browser_tabs_context` 列出当前标签页（id、标题、URL、
   连接状态）——任何浏览器任务的第一步。没有合适的 tab 就 `browser_tabs_create(url)`。
2. **导航/读页面**：`browser_navigate(url, tabId)` 跳转；`browser_read_page(tabId)`
   读可交互元素结构（带 ref，可直接用于点击/输入）；`browser_page_text` 取纯文本。
3. **交互**：`browser_computer(action, tabId, ...)` 做点击/输入/滚动/拖拽/截图
   （用 ref 或 coordinate；点击前先截图或 read_page 确认坐标）；`browser_find`
   定位文本；`browser_form_input(ref, value)` 填表单。
4. **取证**：`browser_network(tabId)` 看请求记录，`browser_console(tabId)` 看日志，
   `browser_js(text, tabId)` 执行自定义 JS（结果以 JSON 返回）。

## 规则

1. **截图确认**：点击前先截图或 read_page；点元素中心，不点边缘。
2. **敏感域**：命中敏感域名时权限层会请求用户确认，属预期行为，不要试图绕过。
3. **移交**：任务需要用户手工完成时（登录、验证码），`browser_handoff(tabId, note)`
   把标签页交还给用户，说明要做什么；用户操作完会自动接回。
4. **用完即止**：任务结束就汇报结果；不要额外开标签页或留后台操作。
