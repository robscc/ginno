# 升级指南（Upgrade Guide）

> 适用：从任意旧版本升级到新版本。当前升级方式为**手动覆盖安装**；
> 应用内自更新在路线图上（设计见 [`self-update-design.md`](self-update-design.md)）。

## 核心规则（先读这个）

1. **升级前完全退出旧版 Ginno**——⌘Q 退出，不是关窗口。运行中替换 bundle
   会触发已知故障「懒加载 zlib 解压错误」（见
   [`troubleshooting.md`](troubleshooting.md) 症状 1），只能靠重启恢复。
2. **`~/.ginno/` 不动**：升级不迁移、不改写你的数据。所有会话 / 设置 /
   知识库都在这个目录里，覆盖安装后原样保留。

## 标准升级步骤

1. ⌘Q 完全退出 Ginno（菜单栏图标若在，也一并退出）。
2. 到 [Releases](https://github.com/robscc/ginno/releases) 下载新版本 dmg。
3. 打开 dmg，把 Ginno.app 拖进 Applications（覆盖旧版，设置和数据不受影响）。
4. 首次启动右键 → 打开（未公证版本的 Gatekeeper 放行，仅需一次）。
5. 正常使用；如遇异常按 [`troubleshooting.md`](troubleshooting.md) 排查。

## 查看当前版本

设置页显示版本号；或终端：

```bash
defaults read /Applications/Ginno.app/Contents/Info.plist CFBundleShortVersionString
```

## 回滚到旧版本

直接用旧版本 dmg 覆盖安装即可——数据格式向后兼容以同一 `~/.ginno` 为前提，
但**新版本可能写入旧版读不了的增量结构**（checkpoint delta 等）。回滚前若
用过新版产生过新会话，建议先整个备份 `~/.ginno/`，回滚后异常再恢复。

## 已知升级注意事项

| 从 → 到 | 注意 |
|---|---|
| ≤ v0.1.0 → 更新版本 | v0.1.0 未做代码签名（右键打开）；后续版本签名策略以 Release Notes 为准 |
| 任意 → 任意 | 若升级后第一次启动即白屏，确认旧版真的退干净了（症状 2） |

## 未来：应用内自更新（未实装）

规划中的 self-update 方案（`self-update-design.md`，状态：待评审）：外部
接力进程负责「杀掉自己 → 换新 bundle → 拉起新进程 → 恢复原会话」，会话恢复
复用现有断线重连 + checkpoint 重建链路。实装后本指南会缩减为「点一下检查
更新」。
