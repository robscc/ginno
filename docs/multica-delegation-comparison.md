# Multica × Ginno delegated agent 对比研究

> 2026-10-18。Multica 源码:`/Users/chenchuansong/workspace_ext/ext2/lab/multica`(source-available,Go server + Electron desktop + `multica` CLI,agent 执行层 `server/pkg/agent` ≈ 9.3 万行 + `server/internal/daemon`)。Ginno 基线:`delegate_agent` 三后端(claude-code/codex/pi)、adapter 化(`tools/external_agents/`)、异步后台委托 + delegation 子会话回放 + `<ginno_subagent_result>` 信封注入(2026-10-04/05 版本,见 `docs/external-agents-design.md` §9)。

## 0. TL;DR

Multica 是「任务派发给 AI 编码代理」的 C/S 工作台:issue 即工作项,`agent_task_queue` 一行即一次运行,本地 daemon 领取任务后直接 exec 已装的 agent CLI(claude/codex/cursor 等 26 种)。**它与 Ginno delegate_agent 同题但不同形**——Ginno 是单进程桌面应用内的工具级委托,Multica 是跨机器的任务分发平台;claim/租约/Redis 负缓存/云 runtime 这一整套分布式机制不适用。真正值得搬的是它把「CLI 即子进程」这件事做到了极致的工程细节:

- **A 层(直接可搬,低成本)**:① fail-closed 终态判定 + terminal_reason 分类;② CLI 版本探针 + 最小版本门 + 路径自愈;③ idle 看门狗补 timeout=0 的洞;④ usage 归一化契约测试;⑤ 孤儿 delegation 重启清扫。
- **B 层(中期,设计文档已留接口)**:① resume 语义全家桶(拒绝即全新重试、poisoned session 黑名单、context_exhausted 措辞指纹);② steer 运行中注入(claude 双向 stream-json);③ edit 模式 worktree 隔离。
- **C 层(不适用,记录理由防止误搬)**:分布式分发四件套、bypassPermissions 硬编码、密钥服务端下发、云 runtime、attribution fail-closed。

## 1. 架构对位

| 维度 | Multica | Ginno delegate_agent |
|---|---|---|
| 委托形态 | 平台级:issue → task → daemon 领取 → exec CLI,多轮 turn 由评论驱动 | 工具级:模型主动调用 `delegate_agent`,一次性自包含 brief |
| 后端广度 | 26 种白名单(claude/codex/cursor/copilot/kimi/dsh/qoder/qwen/grok/ginno…) | 3 种(claude-code/codex/pi),Protocol 抽象一个 CLI 一个文件 |
| 进度回流 | 统一 `agent.Message` 模型 → 500ms 批量上报 + 首事件带外直发 | stream-json → IR → delegation 子会话转录,完成一次性注入父会话 |
| 权限哲学 | claude **硬编码 bypassPermissions**(真边界 = daemon 所在 VM/容器);codex 沙箱写进每任务 config.toml 而非 argv | 启动前静态钉死(read-only = deny Bash/Edit/Write 的 `--settings` JSON;edit = deny Bash + acceptEdits;pi 用工具表白名单) |
| 代码落点 | 任务跑在 `~/multica_workspaces` 的每任务 env root,worktree 隔离,用户目录永不写 | 直接跑在 workspace 目录,edit 模式原地写 |
| 多轮 | session id 存服务端,claim 回发 PriorSessionID,resume/poisoned 全套 | v1 不 resume,子会话 id 已回传留了接口 |

**一个反直觉的印证**:Multica 对 claude 敢用 bypassPermissions,是因为真边界在 VM;它同时也印证了 Ginno 2026-10-04 的排雷——`--restricted` 已被 CLI 2.x 移除、`--disallowedTools=Bash` 会炸掉延迟工具系统,Multica 干脆不碰这些标志。Ginno 的 `--settings={"permissions":{"deny":[...]}}` 方案在两个产品间是唯一既保工具又真禁 Bash 的路,方向无误。

## 2. A 层:直接可借鉴

### A1. fail-closed 终态判定 + terminal_reason 分类(最高价值)

Multica 规则:**没有显式 `result` 事件就不算成功**,进程干净退出≠成功(`pkg/agent/stream_json_result.go:89`);带 terminal_reason(如 `prompt_too_long`)时优先于 is_error 上报。无结构化字段的 CLI 在完成边界对 ≤320 字符 output 做**措辞指纹匹配**识别上下文耗尽。

Ginno 现状(`tools/external_agents/claude_code.py:139`):流被截断(无 result 事件)时拼接 assistant 文本**兜底为 success**——上下文溢出型截断会被静默标记成成功,父模型拿到半截回答还以为任务完成了。

建议:保留兜底(单轮 UX 上全损不如半损),但①无 result 事件的 success 头加 `note=stream-ended-without-result` 溯源;②识别 `prompt_too_long`/上下文溢出措辞时改判 error 并带 terminal_reason,供未来 resume 层决策。

### A2. CLI 生命周期:探针 + 版本门 + 路径自愈

Multica(`internal/daemon/agents_probe.go:94`、`agents_refresh.go:366`、`daemon.go:1035`):
- 启动探针 + 每 2min discovery tick + 每 10min 版本刷新;`--version` + semver 正则,**最小版本门**(claude≥2.0.0、codex≥0.100.0),版本探针 10s 超时且 ErrWaitDelay 时已识别版本仍 salvage;
- 裸命令名走 `$SHELL -ilc 'command -v'` 登录壳兜底(Ginno `_resolve_cli` 已有同款),缓存 30min、PATH/SHELL/HOME 变化即失效;
- **路径自愈**:pinned path 消失(nvm 原地升级)→ 单飞重解析,候选须过版本门才采纳——原地升级 CLI 无需重启 daemon。

Ginno 现状:`_resolve_cli` 进程内缓存,`GET /api/external-agents` 静态检测;用户 nvm 升级 claude 后,运行中的 Ginno 会一直用旧路径直至重启。

建议:①缓存命中后定期(或委托失败 ENOENT 时)重解析;②`--version` 探测 + 最小版本门,低于门在工具结果里明示「请升级」;③失败一次即踢缓存重试一次再报错。

### A3. idle 看门狗补 timeout=0 的洞

Multica:默认 2h 空闲看门狗(awakeclock,不计主机休眠),工具在飞时用更宽阈值(`launch.go`/awakeclock)。Ginno 的 `timeout=0 = 永不超时`(2026-10-05 加的语义)挂死时进程永驻、并发槽位被占死。

建议:timeout=0 时启用 idle 看门狗(如 30min 无 stdout 事件 → kill + `stop=idle-timeout`),墙钟无限但失声有限。这与 Multica cursor 后台 shell 看门狗(PID 认领 + 在飞计数喂狗)同思想,Ginno 只需最简版:读管道字节的空闲计时。

### A4. usage 归一化契约测试

Multica 有 `usage_contract_test.go` 钉死各 CLI 的归一化语义(reasoning 是 output 子集非叠加、cache-inclusive 重分桶)。Ginno 的 usage 映射散在三个 adapter 里(claude `input+cache_*`、pi `input+cacheRead`),只有 fixture 测事件解析。建议:每后端一组「给定原始 usage JSON → 断言 ginno 口径三元组」的契约用例,CLI 升级口径漂移时在此显式失败。

### A5. 孤儿 delegation 重启清扫

Multica 用磁盘 outbox + 服务器核对终态防「任务做完报告丢了」。Ginno 单进程无此问题,但有镜像洞:**应用退出/崩溃时 running 态的 delegation 子会话永远停在 running**,骨架会话和并发槽(`_ACTIVE_DELEGATIONS` 在内存)泄漏。仓库已有同构先例:定时任务中断自动续跑(e05d162,重启后窗口内孤儿 running 行接回进程)。建议照此先例:启动时扫 `type="delegation"` 且 running 的子会话,进程内无对应活进程 → 标 `stop=interrupted` + 诊断行,转录按已有事件归档收尾。

## 3. B 层:中期方向(设计文档 §1.3 已留接口)

### B1. resume 语义全家桶

Ginno 已捕获 `meta.session_id`,resume 只差 argv。Multica 的经验是 resume 的难点不在 `--resume` 本身而在失败语义:
- **resume 被拒**(`resumeWasRejected` 关键词分类)→ 当轮内换全新 session 重试一次,旧 id 上报退役,服务器不再指向它;
- **poisoned session**(`poisond.go`):resume 必然复现失败的 session(上下文溢出进历史、resume 响应超行缓冲)进黑名单,强制全新;
- **context_exhausted**:结构化 terminal_reason 优先,措辞指纹兜底(与 A1 同一机制)。

建议 Ginno 落地 `continue` 参数时的配套:`--resume <id>` 失败即全新重跑并在结果头注明「resume 被拒,已全新开始」;poisoned 列表记在 delegation meta 里即可,无需独立存储。

### B2. steer:向运行中的委托注入补充指令

Multica:评论指定 steer_task_ids → `task_supplements` 表 → daemon 5s 轮询 + provider 侧注入(`pkg/agent/claude_supplement.go`,依赖 stdin 保持打开的双向 stream-json + control_request 协议)。

对 Ginno 的意义:当前委托是 fire-and-forget,父模型发现方向错了只能等结果回来再开一轮。若要支持,claude 后端改 `Popen(stdin=PIPE)` 不关 + `-p --input-format stream-json`,新增 `steer_delegation(delegation_id, text)` 工具或前端按钮。成本中等(协议双向化 + 注入 API + 前端),收益取决于委托时长的分布——先收集真实委托的 duration 分布再决定优先级。

### B3. edit 模式 worktree 隔离

Multica 对**用户本机仓库**的处理(local_worktree.go):快照回放未提交状态(上限 2000 文件/200MiB)建 worktree,用户目录零写入,任务结束把产出 commit 成分支,冲突的未合并编辑下轮再提供。这是它 edit 安全性的真正来源(比权限标志更硬)。

Ginno edit 模式目前原地写 workspace,注入面全靠「子代理无 Bash」挡 `curl|sh`。若上 worktree:edit 委托在 `~/.ginno/delegation-worktrees/` 下进行,完成后报告分支名,由用户/父会话决定 merge——安全性最好但改变 UX(改动不在眼前)。折中:作为 `mode="edit-branch"` 第三档加入,不动现有 edit。

## 4. C 层:不适用(防误搬)

| Multica 机制 | 为什么不搬 |
|---|---|
| batch claim + start 幂等 + prepare-lease 续租 + 终态 outbox「可靠分发四件套」 | 解决的是「多 daemon × 服务器 × 网络分区」的恰好一次分发;Ginno 委托是单进程内的 Popen,内存并发闸门已够。孤儿清扫(A5)是其单机投影 |
| Redis 空 claim 负缓存 / WS hub / `FOR UPDATE SKIP LOCKED` | 同上,无分布式成分 |
| claude bypassPermissions + IS_SANDBOX 预检 | 真边界是 VM;Ginno 的边界是用户本机,必须保持静态钉死 |
| 密钥服务端下发(custom_env + blocklist)、每任务 CODEX_HOME symlink auth | Ginno 外部代理自带账号,单用户桌面无密钥托管需求 |
| 云 runtime / runtimeapps ConnectedApp MCP overlay | 产品形态不同 |
| attribution fail-closed 人类归属瀑布 | Ginno 用量已按 session 归属;workflow 无头路径不记账是已知取舍(设计文档 §6.3) |
| autopilot / issue_wakeup(PR merged 唤醒) | 事件驱动任务触发是 Ginno scheduler 的另一个议题(与 odw 对比结论同:事件触发器是共同空档),不属于 delegate_agent |
| plugin_hook_mcp 服务器中转、daemon 侧 redact 后上传 | 数据不出本机,redact 无对象 |

## 5. 互相印证的坑(两边都踩过/都防了)

- **`--restricted` 已死**:Ginno 2026-10-04 真机矩阵确认;Multica 干脆全程 bypassPermissions + VM 边界。两边都没有找到「保留工具 + 禁 Bash」的 flags 路,Ginno 的 `--settings` deny JSON 是已知唯一解。
- **codex 沙箱的 macOS 代价**:Multica 因 Seatbelt 丢 network_access 从 workspace-write 回退 danger-full-access(MUL-5578,产品决策=真边界在 VM)。Ginno 的 `--full-auto` 映射在 macOS 上实际也是接近全权限,应在文档/UI 明示「edit 模式的真边界是无 Bash 而非沙箱」。
- **长 prompt 别走 argv**:cursor 后端 prompt 走 stdin(#5649,Windows 命令行重切分)。Ginno 的 prompt 恒为末位 argv 参数,超长 brief(几千 token)在 Windows 上有同款风险;macOS ARG_MAX 也有限。低成本防御:prompt 超 N KB 转 stdin(claude/codex 均支持)。
- **stream 解析的防漂移**:Multica 对 codex 用宽松 JSON-RPC 解析 + 缺 usage 回读磁盘 rollout 补;对终态语义用契约测试钉死。与 Ginno「adapter fixture + schema 漂移显式失败」同思路,验证了这套维护模式。

## 6. 建议落地顺序

> **2026-10-18 更新:A 层已全部实施**(实施记录 docs/external-agents-design.md
> §9.7;4 个 delegation 测试文件 67 例全绿):
>
> - A1 ✅ claude adapter fail-closed + `_CONTEXT_EXHAUSTED_RE` 指纹 + `terminal_reason`
> - A2 ✅ `cli_version()` 探针 + `_MIN_CLI_VERSIONS` 门([warning] 明示不硬拦)+ `invalidate_cli` ENOENT 自愈重试
> - A3 ✅ `run_delegation` 统一流式监督(同步路径原来无监督)+ timeout=0 的 30min idle 看门狗
> - A4 ✅ `tests/unit/test_external_agents_usage_contracts.py`;顺带修了 codex 降级路径丢 usage 的真 bug
> - A5 ✅ pid 落 meta + 对账身份复核处置存活孤儿 CLI + 转录中断注记
>
> 剩余:B1(resume)/B2(steer)/B3(edit-branch worktree)按下述原顺序待裁定。

1. **A1**(终态判定 + terminal_reason)——半天级,直接减少「假成功」;
2. **A5**(孤儿 delegation 清扫)——有 e05d162 先例,模式现成;
3. **A2**(版本探针 + 缓存失效)——打包版用户 CLI 升级是真实高频场景;
4. **A3**(idle 看门狗)——给 timeout=0 补语义;
5. A4 顺手;B1 等 resume 需求真实出现;B2/B3 先收集数据再裁定。

## 附:Multica 侧关键文件索引

| 主题 | 文件 |
|---|---|
| 后端白名单/dispatch | `server/pkg/agent/agent.go:369,439` |
| CLI 探针/版本刷新/自愈 | `internal/daemon/agents_probe.go:94`、`agents_refresh.go:366`、`daemon.go:1035`、`config.go:1325` |
| claude argv/双向流 | `pkg/agent/claude.go:1078`、`stream_scanner.go`、`stream_json_result.go:89` |
| codex app-server/沙箱 | `pkg/agent/codex.go:4186`、`internal/daemon/execenv/codex_sandbox.go:98` |
| 任务模型/状态机 | `migrations/001_init.up.sql`、`055_task_lease_and_retry.up.sql`、`internal/daemon/types.go`、`daemon/prompt.go` |
| claim/派发 | `internal/daemon/daemon.go:5282(pollLoop)`、`client.go:297`、`service/task.go:1259,5986`、`service/empty_claim_cache.go` |
| steer/注入 | `service/comment.go:1965`、`daemon/task_supplement.go`、`pkg/agent/claude_supplement.go` |
| worktree/gc | `internal/daemon/repocache/cache.go`、`local_worktree.go`、`gc.go`、`diskusage.go` |
| resume/poisoned | `handler/daemon.go:3031`、`internal/daemon/poisond.go`、`context_exhausted_test.go` |
| attribution/usage | `internal/attribution/attribution.go`、`task_usage` 相关 migration 073/101/102 |
| 特殊处理 | `claude_plugins.go`、`plugin_hook_mcp.go`、`codex_compaction_annotation.go`、`cursor_*watchdog*.go` |
