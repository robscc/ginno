# Ginno 子代理标签

在 Magpie 的「路由」和「用量」页显示 Ginno 子代理请求。插件用 `x-session-id` 查询同一台机器上的 Ginno 会话详情，为 `type="subagent"` 的请求登记用途，并在转发给模型供应商前移除临时标记。

## 安装和卸载

已验证 Magpie `0.1.4730`。插件使用 Magpie 的请求 Hook 和 JS 网关中间件。Ginno 运行时默认地址为 `http://127.0.0.1:8787`，使用自定义端口时可在 `identity.hooks.js` 调整地址。

在 Ginno 仓库根目录运行以下命令。Magpie 会从该路径加载插件，请保留这个检出目录。

```bash
magpie plugin add "$PWD/integrations/magpie/ginno-subagent-label"
```

在同一目录卸载。

```bash
magpie plugin rm "$PWD/integrations/magpie/ginno-subagent-label"
```

macOS 桌面版可用 `/Applications/magpie.app/Contents/MacOS/magpie` 调用 CLI。

## 标签行为

子代理请求的用途记为 `collab_spawn`，Magpie 会将其归为辅助调用。路由组的 `subagent` 条件仍使用 Magpie 的原生子代理判定。
