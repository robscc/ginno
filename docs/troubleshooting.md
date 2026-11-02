# Troubleshooting（故障排查）

> 按症状索引。先看「日志在哪」，多数问题日志里有一手证据。

## 日志在哪？

| 内容 | 位置 |
|---|---|
| **运行时日志（排障首选）** | `~/.ginno/logs/sidecar.log` |
| 会话 / checkpoint 数据 | `~/.ginno/projects/<slug>/sessions/<session_id>.jsonl` |
| 设置 | `~/.ginno/settings.json`、`~/.ginno/config.json` |

日志报错格式：`<时间> ERROR <事件> session=<id> turn=<id>`，traceback 紧随其后。
提 Issue 时请附上相关片段（脱敏后）。

---

## 症状 1：某个功能突然报 `zlib.error: Error -3 ... incorrect header check`
（或 `unknown compression method`；traceback 末尾常见 `pyimod01_archive.py extract`）

**这是头号已知故障，只有重启能恢复。**

- **典型场景**：应用启动正常、用着正常，从某个时刻开始所有懒加载功能
  （上下文压缩、workflow 等）报 zlib 错误。
- **根因**：桌面版 Python 运行时跑在 PyInstaller bundle 里，懒加载时会按路径
  重新打开归档文件。如果 bundle 在应用运行期间被替换（例如边跑 Ginno 边
  执行 `make app` / `make sidecar`），进程内的旧 TOC 偏移会读到新归档的
  垃圾数据 → 解压失败。
- **处理**：**完全退出并重启 Ginno**（⌘Q，不是关窗口）。会话数据不受影响。
- **预防**：构建打包前先退出 Ginno（`make app` 内置了运行中检测防呆，
  `FORCE=1` 可跳过但后果自负）。

## 症状 2：应用窗口白屏，但 sidecar 日志看起来健康

白屏 + sidecar 正常，通常是 webview 网络层被签名问题绊住：`.app` 若只是
**linker-signed**（没有真实代码签名），WKWebView 的 Networking helper 会拒绝
全部请求 → 白屏。

- **自查**：`codesign -dvvv /Applications/Ginno.app 2>&1 | grep linker-signed`
  有输出即中招。
- **处理**：重新用 `make app` 构建（构建尾部有回归检查，会拦截
  linker-signed 产物）；或换用官方 Releases 的 dmg。
- 开发调试请用 `pnpm dev`（runtime 直接跑源码，无 bundle 问题）。

## 症状 3：启动报「端口 8787 被占用」

Sidecar 固定监听 `127.0.0.1:8787`。残留进程（上次未退干净 / 手动跑过
`pnpm dev:runtime`）会占住端口。

```bash
lsof -iTCP:8787 -sTCP:LISTEN      # 找到 PID
kill <PID>                         # 确认是 ginno-runtime / uvicorn 再杀
```

## 症状 4：dmg 装好后打不开（Gatekeeper 拦截）

当前版本未公证，首次启动需 **右键 → 打开**，或在「系统设置 → 隐私与安全性」
点「仍要打开」。仅需一次。介意者可从源码构建（README）。

## 症状 5：升级后行为异常 / 数据「丢了」

- 先确认旧版已完全退出（症状 1 的变体：新旧 bundle 混跑）。
- 会话数据在 `~/.ginno/projects/<slug>/sessions/`，按 JSONL 追加——升级不
  迁移、不删除。若侧栏会话不见了，检查是否换了 macOS 用户 / `~/.ginno`
  被移动过。
- 仍异常：保留 `~/.ginno`，删除应用重装最新版；再不行带日志提 Issue。

---

## 开发者构建问题

**`make app` 拒绝运行**：检测到 Ginno 在跑或 8787 被 sidecar 占用。退出后
重试；脚本场景用 `FORCE=1`（前提：你已确保进程退出）。

**PyInstaller 阶段一屏 WARNING**：Makefile 已过滤 5 条已知良性警告
（langchain 可选子包、pandas 可选后端、Windows DLL 误报）。出现**新**警告
请当真，不要加宽过滤。

**web 导出缺 public 资源（如 icon 消失）**：`make web` 尾部有自愈检查会补拷
并 loud-fail；若手动 `pnpm --filter @ginno/web build`，记得核对
`apps/web/out/` 是否含 `public/` 全部文件。

**i18n 检查失败（`make check`）**：en / zh-CN catalog 的 key 集合或 ICU 占位
符不一致。修 catalog 本身，不要放宽检查。
