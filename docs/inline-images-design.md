# 代码生成图片内联展示（inline-images）

> 2026-08-28 · 已实现并通过打包 e2e 验证

## 1. 目标与范围

Agent 通过 `bash` 跑代码（典型：matplotlib `savefig`）生成的图片，自动出现在
**产生它的那一轮对话气泡里**，刷新/重开后仍在。

**语义决策（与需求方确认）**：

- **仅检测 `bash` 工具**执行期间会话工作区内新增/变更的图片。用户自己在终端跑
  脚本生成的图不覆盖（scanner 已抽成 `files/images.py`，后续可挂文件 watcher）。
- **纯展示，不回喂模型**：图片与机器标记都不进入 LLM 上下文（省 token、不依赖
  provider 视觉能力）。模型只知道"生成过图"（工具输出文本）。

## 2. 数据流

```
bash 工具（tools/builtin.py）
  执行前快照工作区图片 {绝对路径 → mtime_ns}（files/images.snapshot_images）
  执行后 diff → 新增/变更图片 → 输出尾部追加机器标记
      <!--ginno-images:["/abs/a.png","/abs/b.png"]-->
    │（随 ToolMessage 落 checkpoint）
    ├─ 【展示】tools 节点后 _tool_file_effects（api/stream.py）解析标记：
    │    classify=="image" → 注册 files.json + artifacts.json(kind="image")
    │    → WS 广播 image.emit {file_id, name, mtime}（+ artifacts.changed）
    │       └─ 前端 applyBlock 追加 {kind:"image", fileId, mtime} 块
    │          → ImageGallery/Lightbox（URL = fileDownloadUrl(fileId)?t=mtime）
    ├─ 【持久锚点】agent 节点（graph.py）把本轮标记提升进
    │    AIMessage.additional_kwargs["ginno_images"]
    │    —— microcompact 只清 ToolMessage 正文、不碰 additional_kwargs，
    │       锚点在保留窗口内持久；/history 据此重建 image 块
    └─ 【模型隔离】agent 节点对 send-only 副本 strip_tool_image_markers，
         与 strip_old_images 同语义：落盘保留、发送剥离
```

**前端 URL 由客户端拼接**（`blocks.tsx imageUrl()`）：历史块只携带
`{fileId, mtime}`，经 BASE-aware 的 `fileDownloadUrl` 生成同源下载 URL，
`?t=mtime` 破解同名图重新生成时的浏览器缓存。服务端不知道客户端 origin
（dev 下 web:3000 与 runtime:8787 可分离），因此**绝不**在块里直接放 URL。

## 3. 持久化与边界

| 场景 | 行为 |
|---|---|
| 刷新 / 重开应用 | `/history` 读 `ginno_images` 锚点 → image 块（自注册缺失的登记项） |
| turn 中途崩溃（锚点未写入） | 回退路径：解析 ToolMessage 标记（`_messages_to_ui` tool-fold 分支） |
| microcompact（~3 轮外清 ToolMessage 正文） | 锚点在 `additional_kwargs`，不受影响 |
| compaction（整轮总结） | 图随轮一起被总结；文件本体仍在 Artifacts/会话文件面板 |
| 同名图重新生成 | `registry.register` 幂等更新；`?t=mtime` 让前端拿到新字节 |
| 图片被删 | 历史重建跳过不存在的文件；Artifacts 面板显示缺失 |

## 4. 改动清单

**运行时**
- `files/extractors.py`：`_KIND_BY_EXT` 增加 png/jpg/jpeg/gif/webp/bmp/svg → `"image"`；
  `IMAGE_EXTS`；`extract()` 报错文案不再把图片扩展名列进"支持解析"。
- `files/images.py`（新）：快照/diff + 标记编解码（`snapshot_images` /
  `diff_images` / `encode_images_marker` / `parse_images_marker` / `strip_images_marker`）。
- `tools/builtin.py`：bash 前后快照 diff，追加标记。
- `api/stream.py`：`_tool_file_effects` 解析标记 → 注册 + `image.emit`；
  调用点改传原始 ToolMessage content（标记未被 `_tool_content_str` 剥掉前）。
- `graph.py`：`strip_tool_image_markers`（send-only）+ `collect_turn_images`；
  agent 节点写 `additional_kwargs["ginno_images"]`；system prompt 注明图片自动上屏、
  不要 `attach_ref`。
- `api/messages_ui.py`：`_tool_content_str` 剥标记；`_gen_image_blocks`
  解析锚点/回退标记 → `{kind:"image", fileId, name, mtime}`，气泡内按 fileId 去重、
  统一追加在气泡末尾；`_messages_to_ui` 新增 `project_slug`/`session_id` 参数。
- `api/sessions.py`：history 端点透传 slug/session_id。

**前端**
- `blocks.tsx`：Block image 变体扩展 `fileId/name/mtime`；`imageUrl()` 解析；
  InnerBlocks/UserBlocks 改走 `imageUrl`；FileChips 图片图标。
- `ChatStream.tsx`：`applyBlock`/`handle` 新增 `image.emit` 分支；
  `payloadFromBlocks` 跳过无 data-URL 的 image 块。
- `SheetViewer.tsx`：image 分支直接 `<img>`（不走预览 API）。
- `ArtifactsPanel.tsx`：image kind 图标/文案/可点击预览/下载。
- `store.tsx` / `types.ts`：`PreviewFile.mtime`、`FileEntry.mtime`、kind 注释。

## 5. 验证

- 单元：`tests/unit/test_generated_images.py`（标记、快照、bash 检测、
  graph 辅助、历史重建）。
- API/e2e：`test_files_ws.py::test_bash_generated_image_surfaces_inline`
  （真实编译图 + WS + checkpoint：事件/登记/历史/模型隔离/字节下发）。
- 打包 e2e：`test_packaged_ui_playwright.py::test_packaged_ui_code_generated_image_inline`
  —— 真 PyInstaller bundle + 真 Chromium：内联渲染、`naturalWidth>0`（浏览器真解码）、
  标记不泄漏、刷新后仍在。
- 生产冒烟：`make app` 重启后，真实模型（qwen3.8-max）跑 bash 生成 PNG，
  `image.emit` / 历史 image 块 / Artifacts 登记 / 字节下载全部通过，日志零错误。

## 6. 后续可选

1. 文件 watcher 兜底：用户在终端跑脚本生成的图也自动上屏（无活动 turn 时的
   气泡归属需设计，可能走 `preview.emit` 或独立提示）。
2. 视觉回喂：把生成图作为 image block 发给模型自检图表（需开关 + 视觉能力探测）。
3. 多图/大图体验：画廊分页、懒加载、缩略图降采样。
