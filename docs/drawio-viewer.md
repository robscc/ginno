# draw.io 本地预览资源

预览使用本地 vendored diagrams.net GraphViewer。现有文件由 Ginno 提交 `532d9472` 引入，资源内的 `EditorUi.VERSION` 为 `32.7.0`。本地原始脚本的 SHA-256 为 `5a6a873c302ad054262ff9b0e58a3ee9a65c130eca43cd14866fe9ef79f31638`。官方 viewer 下载地址对应滚动版本。审计时返回 `32.7.5`，因此以仓库引入记录、脚本内版本及内容哈希标识本地副本。 [官方 viewer](https://viewer.diagrams.net/js/viewer-static.min.js)

上游 draw.io 项目采用 Apache License 2.0。许可证副本随静态资源分发，gzip 解压后保留原有 JavaScript 字节及其中的第三方许可声明，包括 DOMPurify、regenerator-runtime 和 pako。 [上游许可证](https://github.com/jgraph/drawio/blob/dev/LICENSE) · [随包许可证](../apps/web/public/vendor/drawio-LICENSE.txt)

| 入口 | `/vendor/viewer-static.min.js` 的来源 |
| --- | --- |
| `next dev` | Next.js 直接服务 `apps/web/public/vendor/` 下的原始脚本 |
| `next build` 静态导出与源码 sidecar | `apps/web/out/vendor/` 下的原始脚本，普通静态服务器可直接服务 |
| `make runtime` 与桌面资源 | PyInstaller 收集 `web_out` 后用 `gzip -n -9` 替换包内原件。sidecar 通过原 URL 提供 gzip 或解压后的普通 JavaScript |

浏览器打开 `.drawio` 预览时，通过 sandbox iframe 请求同源脚本。自包含文件可离线绘制图形与内嵌图片。Viewer 中仍有可选的远程图片代理、图形模板和数学渲染资源地址。图表主动引用的外部资源仍受网络可用性影响。测试在浏览器层中止全部非本地 HTTP 请求，并验证预览 iframe 的本地图形和 PNG 解码。

## 装包测量

使用同一个 PyInstaller onedir 构建，统计 `web_out` 普通文件的逻辑字节数。基线以原始脚本替回 gzip 文件并扣除本次新增许可证，其他文件保持一致。

| 项目 | 基线字节 | 变更后字节 |
| --- | ---: | ---: |
| Viewer | 4,961,320 | 1,106,172 |
| Viewer 与许可证 | 4,961,320 | 1,117,528 |
| 包内 `web_out` | 20,689,566 | 16,845,774 |

Viewer 缩小 77.70%，包内静态资源净减 3,843,792 字节。该口径对应磁盘上的展开产物。DMG 传输体积未测量。
