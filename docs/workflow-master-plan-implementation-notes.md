# workflow-master-plan 实施笔记

## 实施顺序
- [x] Round 1 后端：① loop on_empty + loop_skip/loop_cap + skipped 状态
- [x] Round 1 后端：④ emit() 打 ts
- [x] Round 1 后端：③ 硬 deny（workspace-aware）
- [x] Round 1 后端：doctor.py 规则引擎 + API 端点
- [x] Round 1 后端：node_exit usage 遥测
- [x] Round 2 后端：② ExtractNode + 注入器 + LoopNode back-edge + validate + steps_from_dsl(include_extracts) + build_model_by_name + 归因解析 + 提示词
- [x] Round 2 后端：synthesis 记录三件套 + outcome 回填 + cases/stats/replay API
- [ ] Round 3 前端：skipped/loop_skip/loop_cap 样式 + doctor 面板 + 耗时列 + 时间线过滤 tabs
- [ ] Round 3 前端：Settings 总结质量 tab（指标 + case 列表 + 侧抽屉 + replay）
- [ ] 完整性测试（新增测试用例 + 全量回归）— 后端已全过（688）
- [ ] make app 构建
- [ ] commit + push

## Deviations
（记录偏离文档的决策）
- §2.2 steps_from_dsl：计划原意「run.steps 不含 __extract 保持干净」，但这会让
  update_step 的步骤级 run 状态重算在产出步骤完成时就提前把 run 置 done（extract
  节点尚未运行/可能失败却无法拉回）。已改为 `steps_from_dsl(d, include_extracts=True)`
  在 create_run 时把 `<id>__extract` 计入步骤（标题「提取结构化输出（keys）」），
  保证状态重算正确；失败归因仍把 error_detail.node_id 指回源步骤。
- §2.3 硬 deny：不能无脑 deny 整个 home —— 真实 session workspace 就在
  `~/.ginno/projects/<slug>/sessions/<id>/` 里。改为 workspace-aware：
  凭据目录（~/.ssh、Keychains）无条件拒绝；home 仅在「目标路径不在当前 workspace
  内」时拒绝；且 workspace==home（workflow cwd fallback）不予豁免，保住事故场景的
  防护。bash 用 path-token 扫描走同一 _path_denied（弱防护，完整策略待 decision-6）。
- 新增 `models.build_model_by_name`（extract_model 单字符串 → provider 解析）。
- 新增 `workflows/synthesis.py`（case 目录记录）+ `api/workflows.py` 内
  cases/stats/replay 端点（未单列 router，直接挂在 workflows router）。
