---
name: todo
description: 快速管理用户的每日 TODO 清单（增删改查、完成、关联产物）。Use when the user wants to add/list/edit/complete/delete TODO items.
trigger: both
tools: [todo_list, todo_create, todo_update, todo_done, todo_delete, todo_link]
---

# TODO 管理 (Daily TODO manager)

用户的每日 TODO 清单显示在右侧面板（TODO tab）。你通过 `todo_*` 工具直接读写它；
改动会实时刷新到界面上。

## 操作映射

| 用户意图 | 工具 |
|---|---|
| 查看清单 / 有哪些待办 | `todo_list` |
| 新增 / 记一下 / 加个任务 | `todo_create(title, priority, category, due, emoji, tags)` |
| 修改标题/优先级/分类/时间/图标/标签 | `todo_update(todo_id, ...)` |
| 完成某项 / 标记做完 | `todo_done(todo_id)` |
| 重新打开（取消完成） | `todo_done(todo_id, done=false)` |
| 删除某项 | `todo_delete(todo_id)` |
| 把产物关联到某项 | `todo_link(todo_id, artifact_id=...)` |

## 规则

1. **先读后写**：除了明确的新增，先用 `todo_list` 找到目标条目的 id（每行以 `[id]` 开头），
   再做 update/done/delete。用户用标题描述任务时，按标题模糊匹配；有歧义就问用户。
2. **批量操作**：用户说"完成所有 X"/"删掉已完成的"时，逐项调用对应工具，最后汇总一句。
3. **新增时的默认值**：
   - `priority`：根据措辞判断 —— 紧急/ASAP → high；普通 → medium；有空再/以后 → low。
   - `emoji`：给新条目选一个贴切的 emoji（如 🐛 bug、📝 文档、🔍 调研、🚀 上线、📊 数据），让清单一目了然。
   - `tags`：提取 1-3 个简短标签（如 review、docs、q2），用空格分隔。
   - `due`：用户提到时间就填（如 "14:00"、"Tomorrow"、"EOD"）。
4. **关联产物**：如果这次任务产出了文件/文档/报表（artifact），用 `todo_link(todo_id, artifact_id=<id>)`
   把它挂到对应 TODO 上，用户点击该 TODO 就能直接跳到产物。artifact id 可在上下文或
   Artifacts 面板中找到；找不到就不关联，不要编造 id。
5. **确认语气**：操作完成后用一句话简要确认（"已添加 3 项"/"已完成「xxx」"），不要罗列整个清单，
   除非用户要求查看。
