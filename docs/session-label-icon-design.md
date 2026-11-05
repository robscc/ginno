# Session 标签与图标（Label/Icon）产品设计 — MVP

日期：2026-11-05 ｜ 状态：待评审
诉求：会话可手动打 label（文字标签）与 icon（图标），icon 显示在左侧 session
清单行上，用于肉眼快速区分「这条是干什么的」（如 🔧 构建 / 🐛 排障 / 📋 方案）。

## 0. 目标与非目标

- 目标：低成本的会话视觉分类——一个 icon + 最多 3 个 label，纯用户手动，无自动推断。
- 非目标：智能自动打标（可二期基于会话摘要建议）、跨会话搜索 by label（现有
  会话搜索已覆盖标题/内容，label 顺带可搜即可，不做专门过滤器）。

## 1. 数据模型

复用 session meta（`session_meta.py` 的 `_index.json` 条目），新增两个可选字段：

```json
{ "label_icon": "wrench",    // 枚举 id（见 §2），null = 未设置。实现用
  "labels": ["release", "v0.1.2"] }   // label_icon 而非 icon——meta 的 icon 键
                                       // 是 agent 图标快照（api/sessions.py:664），
                                       // 覆盖会腐蚀 agent 归属展示；API body
                                       // 仍为 {icon, labels}，仅存储/透出名不同
```

- 写路径走既有 `patch_session_meta`（_IDX_LOCK + 整文件重写，成熟通道）。
- 新增 API：`PUT /api/sessions/{id}/tags` body `{icon?, labels?}`（全量覆盖该
  两字段；空数组/null 即清除）。`GET /api/sessions` 列表项透出两字段。
- 迁移：无（新字段缺省即旧会话无标签，天然兼容）。

## 2. Icon 集合（受控枚举，不是自由 emoji）

受控原因：① 风格统一（全部走 lucide-react 线性图标，与现有 UI 一致）；
② 避免五颜六色 emoji 破坏侧栏视觉；③ 后续可按 icon 聚合统计。

| id | 图标 | 默认色 | 语义示例 |
|---|---|---|---|
| `wrench` | Wrench | 灰 | 构建/工程 |
| `bug` | Bug | 红 | 排障 |
| `doc` | FileText | 蓝 | 文档/方案 |
| `release` | Rocket | 绿 | 发版 |
| `research` | Search | 紫 | 调研 |
| `chat` | MessageCircle | 灰 | 闲聊/问答 |
| `warn` | TriangleAlert | 琥珀 | 风险/待办 |
| `star` | Star | 金黄 | 收藏/置顶感（不参与排序） |

MVP 固定 8 个；设置项不做自定义映射（二期再议）。

## 3. UI

### 3.1 侧栏行展示（SidebarSessionRow）

```
[icon] 会话标题（truncate）  label¹ label²     ▸子数
  ↑                              ↑
 lucide 14px 着语义色        圆角小 chip，11px，
 位于标题左侧（替代首字符       最多显示 2 个 + "+N"
 位置；无 icon 不占位）          hover title 展示全部
```

- icon 在标题前（余光可辨，不与右侧的 running/waiting 指示冲突）。
- label chip 靠右、子会话折叠箭头之前；超过 2 个折叠为 `+N`。
- 归档区/搜索结果行同渲染逻辑（同一组件）。

### 3.2 设置入口（两个）

1. **行内菜单**：session 行 hover → `…` 菜单新增「标签与图标」→ 弹小面板：
   - icon：8 宫格单选（含「无」）；
   - labels：最多 3 个 chip 输入（回车添加、点 × 删除）；
   - 即时保存（PUT），失败 toast。
2. **命令**：`/tag <icon> <label...>`？——MVP 不做，行内面板已够。

### 3.3 详情页头（可选跟随）

会话内 TopBar 下拉标题区同步显示 icon + labels（同数据源，无新状态）。

## 4. 交互细节

- 打标不改排序、不改归组（分组仍按时间/项目）；仅视觉。
- label 全局不查重（不同会话可各自有 "release"）；搜索框输入 `#release`
  前缀时按 label 过滤——MVP 只做前缀语法 + 前端过滤，不做高亮。
- 删除会话连带清理（meta 已删，无残留）。
- 子会话（subagent）不展示 icon/label（归父行管）；worker 派生会话同。
- 性能：列表渲染多 2 个元素/行，100 会话级无感；meta patch 频率极低。

## 5. 测试

- runtime：PUT tags 校验（icon 枚举、labels ≤3/≤20 字符、清除）、列表透出、
  非法值 422。
- web：SidebarSessionRow 渲染（icon/label/折叠 +N/无标签占位）、`#label`
  过滤纯函数、vitest。
- e2e 可选：面板打标 → 行内立即可见。

## 6. 分期

- MVP：本设计全部（8 icon、≤3 label、行内面板、# 前缀过滤）。
- 二期：按 icon/label 的侧栏分组视图、自动建议（基于首条消息/摘要分类一次
  供用户确认）、导出/搜索联动高级语法。
