# Implementation Notes — 上下文目录 M0（context-folders-design.md）

分支：worktree-humming-singing-sunrise · 2026-08-12

## 已实现（对齐设计 §4.7 M0 清单）

1. **目录库**：`context_folders.py`（store + probe + 规则读取）+ `api/folders.py`
   （`GET/POST /api/folders`、`POST /api/folders/probe`、`PATCH/DELETE /api/folders/{id}`）。
   存储 `~/.ginno/folders.json`，原子写（tmp+rename）。
2. **会话挂载**：session meta 新增 `context_folders`（id 列表）+ `primary_folder`；
   `POST /api/sessions` 支持创建即挂载；`PUT /api/sessions/{sid}/context` 全量替换
   （校验未知 id、primary 必须在挂载集合内，否则置空）；`_ensure_session` 重启恢复。
3. **工具层**：`build_builtin_tools(workspace, context_dirs, primary_path)`——
   primary 成为相对路径基准 + bash cwd；ro 挂载对 write/edit 是硬约束（不受
   bypass_permissions 影响）；glob/grep 新增 `root` 参数（限白名单，结果相对 root
   且带 `(root: …)` 头）；`_path_denied` 增加 extra_roots 豁免。范围外访问维持现状。
4. **注入**：`EnvironmentSection` 渲染 `<context_folders>`（访问级/★primary/规则加载态）；
   新 `FolderRulesSection` 注入 load_rules 目录的 AGENTS.md/GINNO.md（单文件 8k、
   总量 24k 截断），纳入 WorldState 快照与 diff（挂载变更下一轮自动通告）。
5. **命令**：`/mount <path> [ro|rw]`、`/mounts`、`/umount`、`/primary <name|clear>`。
   `/mount` 首个挂载自动成为 primary；handler 直接重建会话图，下一轮即生效。
6. **前端**：Settings → 上下文目录页（tab `folders`，含静态路由注册）；TopBar
   `ContextFoldersChip`（挂载列表/★primary/访问级切换/卸载/路径挂载/跳管理页）；
   WS 新事件 `session.context` → store patch，chip 实时刷新。

## Deviations（与设计的偏差与决策）

- **清除 primary 用空串**：`_session_meta_patch` 过滤 None，故 `apply_session_context`
  以 `""` 落盘清除 primary（resolve 端把 `""` 当无 primary）。
- **chip 里的访问级切换是库级属性**：改 ro/rw 影响所有挂载该目录的会话（设计即
  "访问级属于目录库条目"，M0 不做会话级 override）。
- **`GET /api/sessions/{id}`**：会话在内存时返回内存 dict（既有行为），含
  `context_dirs/primary_path` 调试可见字段。
- **probe 文件数上限 2000**、跳过 `.git/node_modules/__pycache__/.venv/venv`
  （避免 API 线程上同步走大目录）。

## 冒烟记录（2026-08-12）

- 单测回归：`test_builtin_tools/test_commands/test_context_plumbing/test_graph_refresh`
  67 passed。
- 工具层功能冒烟（临时 GINNO_HOME）：primary cwd、ro 硬拒写、rw 可写、root 参数
  与越界 root 拒绝、`<context_folders>`/`<folder_rules>` 注入 —— 全过。
- API 冒烟：probe/add/list、创建会话带挂载、PUT context 替换/坏 id 拒绝/primary
  越权置空、磁盘 meta 持久化、**杀进程重启后 WS 恢复挂载** —— 全过。
- `make app` 构建结果：见下（构建完成后补记）。
- **make app 成功**（14:43，Ginno.app + dmg）。完全退出旧进程后启动新包：
  sidecar health OK（:8787）；`/api/folders` 在真实 `~/.ginno` 返回空库；
  `/settings/folders` HTTP 200；产物 JS chunks 含「上下文目录」；旧会话列表
  （7 条，无 context_folders 键）回归正常。M0 验收清单的剩余交互项（TopBar chip
  点击、/mount 实聊）留待真机使用时确认。
