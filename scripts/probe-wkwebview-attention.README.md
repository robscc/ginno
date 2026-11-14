# WKWebView 注意力语义探针

回答一个问题：Ginno 的 `isAppAttentive()` 该不该纳入 `document.hasFocus()`？
（`docs/notifications-design.md` §4 / §10）

## 为什么需要实测而不是查文档

`isAppAttentive()` 决定「用户是否正在看」，进而决定**要不要发 OS 通知**。判据选错
的代价是不对称的：

- 只用 `visibilityState`：若 WKWebView 在「窗口可见但焦点在别的 App」时仍报
  `visible`，则用户切走了却不通知 —— **该弹不弹**。
- 加上 `hasFocus()`：若焦点位于页面内 iframe 时顶层 `hasFocus()` 变 false，则用户
  明明在操作 Ginno（MCP app 卡、代码面板 viewer 都是 iframe）却被判成「没在看」
  —— **不该弹的狂弹**，正是 2026-11-14 通知风暴事故要治的病。

两种错法都会真实发生，取决于 WKWebView 的实现细节，文档没有给出可引用的结论，
所以直接测。

## 跑法

```bash
cd /tmp && mkdir -p wkprobe && cd wkprobe
cp <repo>/scripts/probe-wkwebview-attention.swift probe2.swift
mkdir -p Probe.app/Contents/MacOS
# Info.plist 至少要有 CFBundleExecutable=probe2 / CFBundleIdentifier /
# CFBundlePackageType=APPL / NSPrincipalClass=NSApplication
swiftc -swift-version 5 -O probe2.swift -o Probe.app/Contents/MacOS/probe2
open -W /tmp/wkprobe/Probe.app      # 结果写 /tmp/wkprobe/result.json
```

**必须打成 .app bundle 并用 `open` 启动。** 第一版直接跑裸可执行文件，因为没有
bundle 身份拿不到真正的 frontmost 状态：`hasFocus()` 全程 false、`window.focus`
事件从未触发，整轮数据不可用（这也是本探针存在 v2 的原因）。

运行约 20 秒，期间会短暂把 Finder 切到前台（步骤 05 的对照实验，必需）。

## 实测结果（macOS 15.6 / arm64 / WKWebView，2026-11-15）

| 步骤 | visibilityState | hidden | hasFocus() | activeElement |
|---|---|---|---|---|
| 01 基线（A 为 key） | visible | false | **true** | BODY |
| 02 **焦点移入 iframe** | visible | false | **true** | IFRAME#f |
| 03 焦点移回 body | visible | false | true | BUTTON#btn |
| 04 同 App 另一窗口 B 为 key（A 仍可见） | visible | false | **false** | BUTTON#btn |
| 05 **别的 App（Finder）前台，A 仍在屏上** | **visible** | false | **false** | BUTTON#btn |
| 06 探针 App 重新前台 | visible | false | true | BUTTON#btn |
| 07 A 被 B 完全不透明遮挡 | visible | false | false | BUTTON#btn |
| 08 A 最小化 | **hidden** | true | false | BUTTON#btn |
| 09 A 取消最小化 | visible | false | true | BUTTON#btn |
| 10 App 隐藏（NSApp.hide） | **hidden** | true | true（滞后） | BUTTON#btn |
| 11 App 取消隐藏 | visible | false | true | BUTTON#btn |

## 结论

1. **`visibilityState` 不追踪窗口焦点**：步骤 04/05 里窗口可见但焦点在别处（同 App
   的另一窗口、或别的 App 前台），它都仍报 `visible`。只用它 → 用户切到别的 App
   而 Ginno 窗口还在屏上时，通知被抑制。**「该弹不弹」的缺口确证存在，且是最常见
   的场景**（并排摆窗口、或只是 ⌘Tab 切走）。
2. **`hasFocus()` 在焦点位于 iframe 内时仍为 true**（步骤 02，activeElement 已是
   IFRAME#f）。所以纳入 hasFocus **不会**把「用户正在操作 MCP app 卡 / 代码面板
   viewer」误判成「没在看」。这是纳入的主要风险，实测排除。
3. **两者都不可省**：步骤 10 里 App 已隐藏（`visibilityState=hidden`）但
   `hasFocus()` 仍滞后报 true。所以判据必须是 `visible && hasFocus()` 的**合取**。
4. 附带发现：被**同 App** 的不透明窗口完全遮挡时 `visibilityState` 仍是 `visible`
   （步骤 07），WebKit 的遮挡追踪对同 App 窗口不生效；最小化与 App 隐藏才会翻
   `hidden`（步骤 08/10）。别 App 窗口完全遮挡的情形本轮未单独覆盖，但步骤 05
   已足以支撑结论——现实中「切走但没被完全盖住」是主流场景。

据此 `isAppAttentive()` 采用 `visibilityState === "visible" && document.hasFocus()`。
