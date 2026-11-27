# Changelog

All notable changes to Ginno are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[SemVer](https://semver.org/) — pre-1.0, expect breaking changes in minor
versions.

## [Unreleased]

## [0.5.3] - 2026-11-27

### 长会话

- 会话内查找按实际可见文本匹配，定位时自动展开所属卡片。流式更新保留已选命中。空查找不下载索引，同一历史版本复用索引数据。（#359、#362、#369）
- 转录窗口随阅读位置和查找结果移动。相同 640 轮样本定位最早消息时，挂载行数由 1,280 降至 80。（#360）
- 首屏按完整轮次加载最近历史，工具正文展开时读取。同一 640 轮样本首屏历史响应由 4,343,711 B 降至 35,159 B，减少 99.19%。全文查找、复制、导出及历史 diff、图片和 Mermaid 回放保留。（#361）
- 切回会话恢复阅读位置。首次加载历史失败时显示原因，可原地重试。（#364、#371）
- 重载会话时，最新一轮的短编辑 diff 恢复默认展开。旧轮仍可按需打开。（#373）

### 文件和产物

- 文件引用打不开、系统打开失败、Finder 定位失败或下载失败时，显示原因。图片预览区分解码失败与文件缺失。（#363、#368）
- 已移动的文件可手选重新关联。重启保留待关联记录，连续切换预览时只显示当前文件的响应。（#365–#367）
- 启动迁移只处理符合旧版格式的附件和分析结果，保留工作区及 Obsidian 报告的原路径。（#372）

### 界面

- 恢复聊天操作卡的运行指示，收纳工具错误。关闭的系统提示在流式更新与历史同步期间保持隐藏。（#357、#358）

## [0.5.2] - 2026-11-25

### 聊天和任务

- 自动折叠聊天中的工具步骤、子代理回报及清单更新。失败、已停止和等待回答的回报仍直接显示。操作卡左缘按状态着色，并可关闭流光。
- 清单节点直接显示任务状态。修复转达问答与父会话唤醒竞态、同一次调度重复发送原生通知，以及首次勾选待办时礼花未绘制。
- 恢复聊天中的本机预览链接。文件在工作区外时，提供加入工作区的入口。

### 模型和上下文

- 按模型标签识别 Codex 档位及兼容协议。修复模型热切换及配置并发问题，按请求展示流式思考摘要。
- 压缩进度跟随实际阶段。轮中压缩后的历史回放只显示简短提示，避免大段摘要撑开聊天区。

### 性能和可靠性

- 按动画帧归并聊天增量，按需载入 Markdown 语法高亮。优化会话索引和 checkpoint 尾部读取，重型文件和表格预览移出事件循环。
- 精简 macOS arm64 包内的原生库符号、draw.io 和 Monaco 资源，并排除 pandas 测试数据。
- 串行化 MCP 配置更新，原子提交待办同步账本，修复项目 ZIP 的目标目录解析。
- 热更新单测使用 Homebrew 测试桩。单测失败时显示用例和完整日志的位置。

## [0.5.1] - 2026-11-22

39 commits since 0.5.0。主题：**任务运行面板/停止链路、产品与键盘交互五项、
压缩流程审计修复（三项 P0）、worker 代答（relay）、编辑 diff 体验、托盘与
构建健壮性**。

### 任务总览与停止链路
- 停止请求携带会话身份、支持非默认项目（`_slug_of` 取真实 slug）；乐观移除
  与停止/刷新双失败竞态修复，失败后任务行可见可重试（d3b7c5e，5652464）。
- 徽标轻量计数：面板关闭时只取六类计数（mtime 增量索引，不再周期解析全部
  历史 JSON）；总览收集移出事件循环（d3b7c5e，#266）。
- 重启恢复状态原子落盘（tmp+rename）；托盘 12 帧逐名预检（Make/cargo 共用，
  缺哪帧点名哪个）；按路径触发的 Linux 浏览器冒烟 job，跳过不算通过（#266）。
- 运行状态三档：服务端 phase 真源（执行中/等你处理/已排期），仅剩排期不再
  报"进行中"；任务标题真按钮、键盘序在停止之前（2e018ee，#271）。

### 产品与键盘交互（P1×3 + P2×2 + 验收缺口×2）
- 环境变量设置防误写：首拉失败仅重试不可保存、无修改不可保存、保存失败保
  草稿；设置读写走 jsonStrict，非 2xx 错误体绝不会被当文档写回。
- 工作流：内置项明确徽标无删除按钮；自建删除确认含名称、失败行内可重试；
  新建默认顺序步骤编辑器（不写 JSON 即可建链式工作流），JSON 为高级模式、
  无损才切换、坏节点定位到步骤。
- ConfirmModal 焦点陷阱：默认聚焦取消、Tab 循环不出弹窗、Esc 不误关底层
  面板、关闭回焦触发器；任务行可键盘打开。

### 压缩流程（审计修复，三项 P0）
- turn 中压缩尾窗回退到配对 AIMessage，孤儿 ToolMessage（→模型 400）归零；
  节流改模块级状态并修 langgraph config 注入失效的深层根因（事件/会话感知
  首次真正生效）；forecast 增量排除 context_baseline，压完即再压的循环消除
  （0d7e204，#272，f5e441b）。
- 用量口径：五处聚合跳过合成基线行（TopBar/用量页不再虚增 input/calls）；
  microcompact 保留 1200 字符头部摘录（可配），摘要模型不再面对被挖空的
  原料；机器注入不计用户轮、二次压缩豁免 20000 截断、竞态重试按时序重建。

### Worker 代答（relay）
- worker ask_user 中断透传：订阅方代答（qid 凭证/幂等墓碑/条件注入）、
  relay_answer 默认 allow + 前端折卡片（e2ce562，6bba99d，e4e859d）。

### 界面与体验
- 编辑 diff 全量上线：edit/write/bash 气泡内联红绿 diff，历史默认收起仅最新
  turn 展开（c7edb0a，2e13ef2）；工具参数流式预览 tool.args_delta（0899209）。
- 后台任务卡命令行三件套（点击展开/零延迟 tooltip/复制，be7517d）；上下文
  文件夹下拉自适应 flip 与右缘锚定（1f89fd0，ce1a7d1）；侧栏 A2A peer 展示
  （4978423）；托盘左键菜单、彩虹描边流光、▶N 徽标收敛（e654206，b9675c7，
  3d89bdd）；AIMessage 裸 markdown 图引用修复（d1a6658）；历史编辑等一系
  列任务面板口径修复（6612179，e9dbd99）。

### 运行时与构建
- 无人值守 ask_user 的企微异步人审（e8e3a1b）；lastwords auto 续跑不再误标
  无人值守（e75c9ba）；code 生成图片内联与回放统一化（c473545）；会话 meta
  形状统一修活跃会话模型 chip（16b2456）；子代理回传信封解析正则化
  （950cb94）；tray_anim 缺帧自愈守卫（6df284f）；CI runtime-tests xdist
  并行 10.4→~3.5min（a092f2e）。

## [0.5.0] - 2026-11-21

66 commits since 0.4.1。主题：**安全加固（环回守卫）、流引擎架构化（staged 唯一路径）、
可维护性重构 7 批次收官、menubar 状态导航、远端/连接器可用性系列修复**。

### 安全
- **环回 Origin 守卫 + CORS 白名单化**（5c812e5）：斩断任意网页驱动本地 API——
  非白名单 Origin 的浏览器请求被拒（P0-1）；未知 /api/* 路径 404 JSON 契约恢复
  （17d7b29），connectors 路由 6 处裸 {error} 补 ok:false 包封（7e70a28）。

### 流引擎（架构化）
- **staged 四阶段成为唯一引擎路径**：`_stream_graph` 复制式迁移为 stages/
  （permission/toolloop/streamio/finalize，批次 5-2），golden 帧序契约与旧路
  逐字节一致；逐 turn 六类资源全走 **AsyncExitStack**（批次 5-3，异常路径
  同清，根治三次泄漏事故的结构病）；legacy 路径与 `GINNO_STREAM_ENGINE`
  开关已删除（68854ff）。
- **流停顿重试修复**（8e7b4bf）：首等 60s 保持、停顿重试去重——名义 2 次
  实际只跑 1 次的缺陷消除。
- 直播 turn 收尾帧丢失卡死：周期 turn_state 探针自愈 + 控制帧事件账本（a1618f7）。

### 重构（7 批次绞杀者收官，19 PR）
- 1500+ 行产品文件清零：ws.py 1575→8 模块（session_ws 1417→13 行，消息泵
  单一所有权）、handle.ts 2217→40 行（15 事件组注册表 + CODEMAP 77 事件
  盘点）、sessions/code/commands/mod-broker/CodePanel/PinStream/server.py
  全部拆解；lifespan 启动任务表化（36+10 项声明表，fail_policy 三态）。
- 五套契约快照留库（golden/事件分发 93 用例/命令表/OpenAPI/ws 命门），
  顺带真修复 7 个 bug：coalesce 死设、Dev 丢图、Pin 三连、steer 同 id 重发
  拼双份（19d7cd0）、`_steer_user_arrived` 早崩窗口、薄壳名字漂移、
  event_contract glob 静默空。
- 启动任务 timeout 闸（b5b9590）：32/36 任务三档加闸（60s/120s/300s），
  白名单 4 任务 + shutdown 全表；守卫「快照∪白名单」双断言。
- 共享层 applyBlock 超集化（c4d2cc2）：Pin/Dev/主窗口三端单实现，Dev 抽屉
  白得主版 9 项能力；fake_model 多 tool_call chunk index 修正（b219fd0）。

### 功能
- **menubar 状态与置顶会话导航**（e1378c8）：running ▶N + 下拉快速切换置顶会话。
- 遗言 mode=auto 真自动恢复（0398ff1）+ 信封实时渲染（ea07c95）+ 卡头行不换行（d282375）。
- 配置健康检查引擎 + /api/doctor + 诊断页 Config Doctor（9df9b03/5309aa8）。
- MCP 工具 tab 移入智能体组（2a34eea）；定时任务执行中彩虹点（e2ce738）；
  彩虹=running 语义四处统一（90cfc15/d78b952/2ddb986/b5c036e）；loading 声呐
  脉冲（bafa4cf）；用户旅程批次：侧栏/产物过滤 + 模型空态引导（a6db707）。

### 修复
- **连接器页请求风暴**（6dd5f2f）：report_meta 广播守卫 + 前端节流双修；入站
  toggle 总开关守卫（3530604）；relay 双客户端互踢风暴（cc1b139）。
- 远端/沙盒系列：远程会话 runtime 安全网（eb83c39）、attach_ref 点击装死
  （c74d80a）、turn 生命周期账本（8104e73）、前端守卫（9cafd14）；AgentTeam
  投递竞态改事件驱动 ack（2427ae6）。
- **dist-info 瘦身连环雷**（132cadc/be1e4e8/028c46a）：httpx2/httpcore2 白名单
  + frozen import 健康探针 e2e——定时任务 PackageNotFoundError 根治；
  runtime bundle 瘦身 161→157.7MB（c7c29a6）。
- 调度孤儿 running 行收编只扫近 4 天（942295f，777h 悬挂实锤）；sidecar 启动
  单实例守卫（10edd0b）；dev-swap 旁路自愈记账（93caaaf）；回放分泡新 turn
  焊进旧气泡（74fd5ee）；composer 拖拽上限 1/3（5405b09）；关联面板纯订阅
  语义（979799e）。

## [0.4.1] - 2026-11-18

20 commits since 0.4.0（4 feat / 10 fix / 3 perf / 2 docs / 1 test）。
主题：性能治理（会话首屏分片挂载）、远端/沙盒可用性修复、会话级技能路径。

### 性能
- **会话切换首屏分片挂载**：压低 React mount 长任务（3c0f317），配套三处实测修正
  —— setTimeout 切片、锚点环会话隔离、监听器闭包常新（a681202）
- LLM 请求行默认不渲染（用户反馈「不需要这个」，8bba704）

### 功能
- **会话级技能路径与按需加载**（3ab579a）+ 修 skill_dirs 误禁静态层斜杠展开（ad420d1）
- 远端 turn 全粒度进度转发 + LLM 请求级可观测事件（d1bd02d），远端会话禁用模型选择器
- A2A 对端节点收进 hub 子配置页签，不再占顶层连接器卡位（76eae64）

### 修复
- **沙盒联网两连修**：只读绑入 resolv.conf/hosts 修 DNS 解析（fefc4df）；
  绑入 CA 根证书修 https 全部 exit 77（c04f5b1）
- **远端会话**：turn 收口本地 running 态——修「远端结束了本地还在运行」（002d587）；
  {name} 占位符闪 slug——session 载荷真源解析 remote_peer_name（a31dd23）；notice 精简（42c0cb1）
- **A2A**：入站权限放行面从 guard 派生——修 headless 工具永久挂起（da9a1c8）
- **engine**：移除 llm 帧翻译分支——CI(ubuntu) runtime-tests 卡死元凶（ebed87c）
- 渠道 admin_users 无法从 UI 配置 + 拒绝文案指路错误（63419a4）
- 过时失败恢复横幅在新一轮对话后自动消失（cd18211）
- 渠道只读会话隐藏「立即压缩」建议卡（49bfed0）

### 文档
- Linux 安装指南：ginno node headless 真机部署沉淀 + 模型提供方实证配置（9b20a7a / c8a5ff6）

## [0.4.0] - 2026-11-17

65 commits since 0.3.3（~20 feat / ~32 fix / 3 test / 3 perf / 2 docs）。
主题：A2A 双向协作与远端会话（本次最大块）、上下文压缩体验重构、
session 收件箱语义、运行时性能治理。

### A2A 连接器 + ginno node（外部 Agent 双向协作）

**能力**
- **A2A 连接器 P0**：双向外部 Agent 协作——能力地址接入 + 独立入站 listener
- **ginno node headless**：trust/caps/exec_root 执行侧强制 + 入站执行层
  + 自环互通 + peer_offline
- **SSE 进度流**：派活不再只有一个 WORKING 干等；Card 广告 streaming
- **input-required 远程批准闭环**：headless 节点不再永久挂起（reference_task_ids resume）
- **ginno tunnel**：SSH 隧道守护，不再手敲手接
- **文本产物回传**：修「能干活却看不到成果」
- **远端会话 R1/R2/R3**：turn 可在 A2A peer 上执行 / 远端 peer 当子
  agent / 新建对话可选远端节点执行方
- 入站权限放行面按 trust 派生

**安全**
- 入站 shell/repl/bg_run 全部走 bwrap 沙箱——闭合三个逃逸口
- 外部派活入站一律拒
- 渠道会话 readonly 在 API 层强制——堵 WS invoke/steer/投递绕过
- providers api_key 支持 `env:VAR` 引用——密钥不必落进可读的 settings

**修复**
- A2A 远端 turn 五项（键名崩溃 / notice 字段 / 续答带 referenceTaskIds /
  影子 contexts / httpx 泄漏与超时不取消）
- 远端会话每轮必崩（ContextMap.get_or_none 误传两参）
- SSE 终态帧去重 + 掩码地址不再显示空 #fp

### 上下文压缩体验（预警口径 + 过程硬化）

- **预警口径统一绑定压缩阈值**：圆环/建议卡此前按窗口固定百分比
  （70%/90%）设计，大窗口模型（1M）上阈值只占窗口 50%——预警永远晚于
  触发（倒挂）；现在 `usage.context` 带 threshold/compact_pct，圆环变色
  + 刻度线 + tooltip 三口径，建议卡 0.85×阈值且 turn 运行中可见
- **压缩期间可输入**：发送经 steer 队列在压缩后注入（此前 readOnly 干等）
- **过程硬化**：摘要 30s 进度心跳（有预估时时间轴 pct）+ 无预估
  ease-out 缓动；microcompact 历史重放痕迹行（后台清理重载可感知）；
  断线重连保住「压缩中」状态（turn.state 探针 + _COMPACTING 注册表）
- ContextRing 淡色底座（同色系 8% 实心 + 22% track，随语义档变色）

### Session 收件箱与侧栏

- **标为未读 + 右键上下文菜单**：forced_unread 服务端语义（打开即清），
  右键菜单与扇形菜单动作单源（useRowActions），紫点/Dock/notice 全链
  联动；修标未读后点开不变已读（乐观态漏清）
- running 会话左边缘条改流动彩虹
- 会话切换渲染提速（highlight 延迟 idle + files 请求去重）

### 可靠性与性能

- **lastwords 恢复决策卡**：重启/热切换弹恢复/忽略卡 + 拉取式兜底修
  广播丢帧；收 interrupted 后台任务 + 人工会话任意 reason 挂起待确认
- **记忆自动审批（无人值守）**：夜间蒸馏草稿自动应用 + 备份/安全闸
- **worker digest 超上限改文件传递**：不截断 + 完整展示 + 跨会话可读
- **内存基线治理**：tuple 缓存字节预算 / 会话 LRU / 线程右 sizing 修复
  （anyio 4.x API 路径错）/ RUN_CACHE 加帽
- **空闲态 CPU 治理**：终态鸭静态化 / 隐藏 pill 条件渲染 / 停表与 bailout
- 线程池 sizing 真正生效；正文被工具行拦腰切断（合帧缓冲漏 flush）；
  工具结果错位与「执行命令中」卡死（tool.end 丢配对 id）
- bg_run 不再返回 raw log path（agent 拿去 read_file 撞 home deny）
- goal 生命周期收尾：complete 自动归档 + 运行中设 goal 自动 steer

### 平台与工程

- **版本号单一真源修复**：_version.py 从未被 bump，7 处自报 0.1.0
  （实际 0.3.3）——/api/health、A2A Agent Card、UA 网关识别全错；
  现统一 import `_version.__version__`，CI 钉一致性
- **web lib 补测两批**：26 文件 933 用例，覆盖率 4.91%→69.29% + 门禁上调
- ui-e2e 闸门修复 + macos-14 退役 → macos-15
- 设置页加字体族 + 三档字号
- web_search 一键修复：诊断面板自动安装 SearXNG 替代脆弱 DDG
- PlanBoard 依赖图视图（Workflow 同构横向 DAG）
- team 回帖不再截断 / 协作广场空回复 / live 气泡尾部状态与统计语义修复
- dev-swap 确认框永不出现修复；studio inbox flaky 修复

## [0.3.3] - 2026-11-15

58 commits since 0.3.2（13 feat / 28 fix / 6 docs / 2 refactor）。主题：
AgentTeam 多智能体团队全量交付、上下文管理与 Session Tool Policy、
通知系统根治性重构、模型选择器二级化。

> 版本号按 patch 走，但实际含四个主题的新能力——记账按主题分节便于回溯。

### AgentTeam（多智能体团队）— 本次最大块，backlog 56 项全部交付

**能力**
- **渠道消息投递团队**：钉钉/企微来消息 → 投递给团队成员 → 成员回帖回到 IM
  原线程（P2-3）
- **schedule 驱动团队** + `worker_bus` 放开 team-member（P2-1/P2-2）：定时任务
  可直接点火一个团队
- **workflow 节点调用团队成员** + 团队成本可见（P2-4/P2-5 runtime 侧）
- **成本面板 / 投递 brief 卡 / pin 浮窗联动**（P2-5/P2-13/P2-24 前端侧）
- **投递回执可视化**（P1-1）+ **投递 ≠ 批准**语义分离（P2-11）
- 成员工作台顶栏补三层记忆 chip（P2-12）
- 成员回帖 / blocked 状态接入统一通知体系（P1-18）
- @ 多人并行投递（P1-9）

**修复（P0 全 7 项 + P1/P2 共 28 项）**
- P0-1 广场发送后 composer 永久锁死；重载误判未送达导致重复投递
- P0-2/P0-3/P0-5 Lead 工具不可达、steer 静默丢弃、坏 roster 数据丢失
- P0-4 unknown/error 语义态是死代码 → 改读真实数据源
- P0-6 模板/一句话建队三处断裂 + 领队覆盖时丢成员
- P0-7 + P1-2/P1-3/P1-4 补 turn 生命周期钩子一次修好
- P1-5/P1-6/P1-8/P1-10 并发闸击穿、回帖丢失、领队指针漂移、wait 死等
- P1-7 seen/徽标漂移——落盘改用主窗口视角
- P1-9 顺带修一个实测发现的 **checkpoint 并发写丢失**
- P1-11/14/15/16/17/19 + P2-14~19/25 前端 13 项缺陷
- P1-12/P1-13 @ 菜单键盘绕过 blocked 禁选、steerHint 死代码
- P1-20 广场 AgentChips 语义错位 → 改为投递给成员
- P2-8/P2-9/P2-10/P2-20/P2-22 徽标口径、并发可配、展示文本、wait 语义
- P2-21 多会话回帖读错 session；P2-23 记忆注入补 turn 级断言；
  P2-26 编制写入边角；P2-28 文档与实现对齐
- 团队 packaged e2e 用例（P1-21）

**决策留痕**：TEAM.md 维持与 KB 隔离、goals 暂缓但阻塞已解除（P2-6/P2-7）

### 上下文管理 / Session Tool Policy

- **Session Tool Policy（runtime P0）**：会话级 MCP / Skill 按需禁用
- **账本式面板 + 工具级开关 + tiktoken 计量**（P0）
- **输入框「上下文管理」面板**（Tool Policy P1）
- 二轮改版：去预设档直接勾选 + 列表显示优化
- `McpToggle` 提升为公共 `ui/Toggle`，设置页与面板共用
- 修复：勾选即自动保存（uncheck 重开回 check 的根因）；重开丢勾选（裸 fetch
  跨源拿不到 policy）；breakdown 不可用时降级为估算行而非永久骨架屏；面板视觉
  错乱与状态语义；开关「点不动/只能点一次」；两次快速点击回弹；iOS 开关 knob
  跑出 track 压住名称；工具级开关紫色胖条与名称错位
- 修复：`_ensure_session` 的 tool_policy 赋值挪进 session dict 构建（CI 红）

### 通知系统重构（根治「乱通知」）

- **P0-1 通知风暴根治**：`token.delta` 经 switch fall-through 进了
  `user.question` 的共享 case 体，而 delta 帧没有 `id` → 去重键退化成
  `${sid}:${Date.now()}` 每帧唯一 → **每个 token 弹一条「等你回答」**。实测
  单会话 1583 条、`shell.log` 里 77% 是通知流量。拆独立 case + 抽
  `maybeNotifyAskUser()` + 内容派生去重键
- **P0-2 投递失败 40%**：`show()` 经 `block_on_current` 要求
  `CFRunLoop::is_waiting()` 为真，而主线程正在派发触发它的同一个事件时该值为
  false（**假阴性**）→ 2271 次 `MainThreadNotRunning` 静默丢弃。改有界重试
  （5 次 × 25ms），**不**搬主线程（紧随的 `wait_for_action` 会冻住 UI）
- **P0-3 隐藏悬浮窗是第二通知源**：`/pin` 常驻 hidden → 「用户没在看」恒成立
  → 正盯着主窗也弹、且与主窗重复。闸门下沉进 `notifyNative` 唯一出口
- **P1 闸门收口**：`notifyKind` 设为必填 + 出口统一施加总开关/分类/勿扰/隐私/
  提示音；删除死开关 `subagent`（无生产者）、`workflow` 接真并配
  `kinds_schema=2` 一次性迁移；新增 `schedule`/`bgTask` 分类；reply 两级节流
  合一 + flush 时复检注意力 + 切进会话即 cancel
- **顺带修一个生产 bug**：`fromWire` 浅拷贝导致 `d.dnd.enabled = …` 写穿
  `DEFAULT_PREFS`——勿扰窗设过一次就永久污染模块级默认值，关不掉且重启不愈
- **P2**：补回权威 spec `docs/notifications-design.md`（V2 那份被实现它的提交
  删除，注释长期指向不存在的文件）；`notice-unification` 的 Rust 通知窗标
  **ARCHIVED**（会造出第三条渲染路径 + 重新引入多窗口重复）；`shell.log` 加
  毫秒时间戳与 `notify_kind`、带锁 append（消除 529 行粘连）
- **`hasFocus` 实测定案**：自打 WKWebView 探针逐步制造 11 种窗口状态，证实
  `visibilityState` **不追踪焦点**（切到别的 App 而窗口仍在屏上时仍报
  `visible` → 该弹不弹），而焦点位于页面内 iframe 时顶层 `hasFocus()` 仍为
  `true`（纳入安全，不会误判 MCP app 卡 / 代码面板 viewer 的使用）→ 判据改为
  合取 `visible && hasFocus()`
- 实测验证：修复后同等负载（4 个后台会话 / 16,938 output tokens / 5分45秒）
  壳侧只收到 **1 条**合法 reply 通知，`show FAILED` 与 `fire-and-forget` 归零

### 模型选择器与思考强度

- **二级化**：一级选模型、二级选思考强度
- 手风琴内联难度——选模型一次点击、难度不跳页
- 选中行加思考强度直达箭头
- 修复：思考档位一次点击即生效（根除并发 PATCH 覆盖竞态）

### 其它

- **压缩进度按历史时长预估 + ETA**（取代盲目缓动）
- 构建：web 目标构建前预检自愈 `src/assets` | `public` 跟踪资产
- CLAUDE.md：补 GraphQL 不可用时的 REST 判绿写法 + `total_count` 第三条件
  （手搓 grep 判绿已翻车三次，最近一次是提前合并了还在 in_progress 的 PR）

### 评估后决定不做（留痕）

- **Code Mode**：原型 + 真实 Ginno 对比（magpie qwen3.8-flash）+ 最小 repl
  接入，实测后设计标注 **ARCHIVED**——真实数据定案不施工
- **工具 schema 按需注入**：实测 9.8k/轮，结论不做通用动态注入

## [0.3.2] - 2026-11-12

Patch 发布（0.3.1 同日追发）。

### 修复
- **SteerBand 长注入折叠**：`[world state update]` / `[turn context]` 等
  上下文注入数千字全量内联渲染成文字墙——超 240 字符折叠为预览 +
  「展开全文」按钮，短注入零变化（中英 i18n）

## [0.2.1] - 2026-11-11

40 commits since 0.2.0。主题：Instincts 本能学习、会话内搜索、通知治理、
遗言确认续跑、热更新取消与自愈、drawio 多页预览。

### Instincts（本能）— 行为规则沉淀
- **P0 最小闭环**：L1 确定性捕获 + YAML 存储 + 模板规则生成 + 注入管道 +
  管理页（左下角 BETA 入口）
- **P1-lite**：retry_recovered 捕获 + 置信度衰减治理
- **L2 纠偏检测**：启发式预筛 + observer 小模型判定（detached 任务化）
- 修复：i18n 目录未注册致页面显示原始 key；收尾钩子线程化修 CI 落盘竞态

### 会话内搜索与提及
- **Cmd+F find bar**：数据层索引 + Highlight API 高亮，跨消息定位
- mention P2：拼音首字母 + fuzzy 匹配排序；chip 图标化（气泡+输入框）
- mention 投递 e2e 轮询补 messages=None 中间态防护

### 通知治理（OS 通知不再刷屏/误报）
- 跨会话 reply 节流：KindRateGate 60s 冷却 + 合并计数（+N）
- worker 假完成修正：linked.done 附 worker_status，working/waiting 不再
  显示「已完成」
- ask_user 挂起通知去重集落 sessionStorage——重载不再重复弹
- workflow/subagent 两类 OS 通知默认 OFF（应用内已有 notice/digest）
- **桌面壳 CPU 修复**：通知等待线程无上限轮询 getDeliveredNotifications
  致 ginno-desktop 58%/usernoted 84%——限 4 路 + fire-and-forget

### 遗言模式（重启恢复）产品化
- auto 模式人工会话改「待确认续跑」：注入带 + runtime.lastwords_pending
  广播 + 收件箱点击直达 + POST /api/lastwords/resume（TTL 1h，exactly-once）
- subagent 会话保持无感直跑；低工作量丢弃日志 debug→info

### 热更新（dev-swap）
- **取消功能补全**：pidfile + TERM 优雅取消 trap + 切换点安全门 +
  命令路径可达（企微/桌面同口径）
- public 资产完整性自检+自愈（fileicons 丢失自动恢复）
- 进度计时修复：elapsed 锚 stageStartedAt，不再每 2-5s 归零
- web 内容戳纳入 public/；补拷防线升级文件数比对

### 文件预览与产物
- **drawio 多页预览**：页签切换（分段药丸控件、横向滚动+N/M 计数、
  ←/→ 键盘翻页、主题自适应配色）
- 文件预览 Esc 关闭；Esc 统一 hook 补齐 7 个浮层（向导类带防误触 guard）
- 产物面板「在 Finder 中显示」（open -R / explorer /select,）
- 附件图标全类型化：vscode-icons 彩色图标替换 emoji 三态

### 安全与权限
- **保护区 own_ws 修复**：挂 primary 挂载后会话自己的 uploads/memory
  目录不再被误拒（_DenyPolicy 独立豁免参）
- G3 渠道命令权限闸：admin 级命令按 userid 判定 fail-closed
- bash 工具环境变量注入：settings.json "env" 键 + 设置页可视化编辑
  （PATH 追加语义，代理场景开箱即用）

### 其他
- 桌面壳开机自启动（SMAppService 注册态为唯一真源）
- 内置 AgentTeam 团队模板（含内阁模式）
- 断线补发窗口 6s→60s（冷启动/休眠恢复）
- 连接器安装向导 i18n 补齐（wecom/vibe/dingtalk 双语）
- 品牌行鸭子 logo 恢复 + 侧栏加宽 w-72
- MCP app 初始数据推送 content 形态规整（trending 空表修复）

## [0.2.0] - 2026-11-09

63 commits since 0.1.3。主题：MCP Apps 端到端打通、通知统一、遗言模式、
企微媒体、切换会话性能治理、rtk 深度集成。

### MCP Apps（聊天内可交互应用卡）
- **P0 全链路**：`_meta.ui.resourceUri` 透传 + `ui://` 资源读取 + 沙箱 iframe 卡
  （McpAppCard/postMessage 桥/tools-with-ui 接线）；wire 协议对齐官方 ext-apps
  （`ui/initialize` 应答含必填 `hostContext`；推送 `ui/notifications/tool-result`）
- 修复：MCP 工具参数类型端到端保真（number/integer/boolean 不再拍平成 string）；
  probe 试连 cancel scope 跨 task 崩溃；向导错误可复制；tools-with-ui 空清单
  15s 新鲜期 + lookup miss 自愈刷新（app 卡不再延迟/缺失）

### 通知统一（右下角唯一出口）
- notice 总线：右下角容器 + 未读/已读通道（mode/silent/countInBadge 防双算）+
  收件箱面板（RightDock 新 tab）+ TopBar 铃铛 badge；ModToastHost 等私有
  toast 全部退役合流；session 未读投影进收件箱

### 遗言模式（重启恢复）
- 被 dev-swap/重启杀掉的 turn 自动恢复：WAL 持续写 + SIGTERM 兜底 + 启动扫描
  （exactly-once/TTL：signal 1h·running 15min/恢复价值门槛）；人工会话
  notify 注入（前端折叠卡 + 通知中心条目 + 设置三选一），subagent 自动续跑
  （via:lastwords）；30 用例质量矩阵

### 企微媒体
- 被动回复 send_image/send_file（协议矩阵查证：image≤10MB 仅 png/jpg/gif、
  file≤20MB、media_id 3 天）；产物上行三段式（大小预检/降级回执/矩阵测试）；
  **正文媒体引用即发**——markdown/attach_ref 中发现的图片文件触发发送，
  push_only 场景降级可达提示

### 切换会话性能治理（988ms → 0.5ms）
- checkpointer 热路径**尾窗读**（尾部窗口 fold，缺锚翻倍回退全量）+
  物化缓存（stat 失效）——get_tuple/put/get_pending_interrupt 三热路径
  从 O(日志) 降为 O(窗口)；前端**转录窗口化**（首屏 ≤80 条，⌘K 锚点自动
  扩窗）；**存量切分器**（`ginno checkpoints segment`：主文件自包含 +
  只读归档段 + 首切备份 + fold 等价自检回滚）

### rtk 深度集成
- bash 超预算中间段流式落盘 + read_file 分页恢复；rtk 复合段感知重写 +
  git -C 形态 + 1.1.0 白名单；同目录探测（嵌入式/评测分发 ginno 与 rtk
  同卷不进 PATH）

### 修复
- ContextRing 混用守卫 + 压缩阈值与圆环同口径（会话级模型解析统一链；
  sideband record 不入分子，13%↔60% 抖动根治）
- 断线窗口发送不再秒标未送达（先等重连 6s 再补发，红叹号兜底）
- 定时任务实例 artifact 预览点不开（SheetViewer 移出 hidden 工作区行）
- 429 限流报错清洗（去重置时间重复/英文括注，附结构化 reset_at）
- scheduler UTC 午夜时间炸弹（reconcile 扫窗动态展开，CI 三用例红根因）
- dev-swap 三连：quiesce 不计后台任务（自锁）、进度锚点+时间插值、
  mktemp 模板 X 末尾
- ask_user 多问题卡选项后自由输入解锁 + /compact 假「未送达」
- 任务事件历史重放折叠（与 live 折卡对齐）；blocked/deny 改 ToolMessage
  配对响应（拦截信息独立渲染）；后台任务 hover 显示完整脚本

### 特性与工程
- 特权会话：运行时数据目录只读挂载（Ginno 承接 Ginno）+ workspace-in-
  mount-roots 打穿 home-deny 历史洞封堵（哨兵断言）
- worker_bus assign_worker 异步化 + 派完自动订阅结论
- BYOM（自带机器）设计文档；CLAUDE.md 增补特性分支 PR 自动化章节

## [0.1.3] - 2026-11-06

- **dev-swap 热更新全链路可视化**：六阶段进度（侧栏品牌行徽标 + hover 六阶段卡 + session 内倒计时/P90 慢告警）、切换确认门（右下角对话框，暂不后 ⏳ 待切换可补完成）、构建取消（左上按钮二击）、`POST /api/devswap/start|cancel`、WS 重连快照兜底
- **issue 清仓（#4-#12/#13-#22，社区审计 by vector4wang）**：
  - 修复：turn 变量遮蔽 UnboundLocalError、prompt hook 改写未入最终消息、workflow resume/continue 预算跨段累计、Code Panel 手动刷新确认、mod-runner 数组 props 丢弃、prefill 事件 sessionId、Extract 预算计数
  - 加固：broker 帧身份以连接为权威 + middleware 改写后最终参数复检；checkpoint delta 同 ID 改写丢失（降级 full）、channel 绕锁（共享 _turn_lock）、多实例 flock+fsync；Pin history 时序合并；manifest 补 nativeMessaging
  - 设计：外部 Agent 只读边界（docs/external-agent-readonly-design.md）；broker store 路径单一来源（Python 计算，Store 单锁快照，PR #23）
- **产品功能**：session 手动标签/图标（8 icon 枚举 + ≤3 label + # 前缀过滤）、侧栏 ask_user 等待图标、修正型注入合并、@result 全库候选、热更新侧栏按钮、AgentTeam Beta 标、usage 压缩次数列
- **修复**：rtk GUI PATH 兜底、diff 行号重复/版本串可读化、toolLabels 旧种子迁移误伤、压缩观测日志、zsh EQUALS 诊断、worker_bus digest 生命周期可观测、bubble_turn_mismatch 洪水
- **工程**：pre-commit 门禁（fmt/ruff/lint 按类型分发）、CI mod-runner job / cargo test --lib / ruff correctness 门禁、architecture.md 修正

## [0.1.2] - 2026-11-05

### Added

- **Turn 级耗时 + token 显示**（对话每个 turn 右下角）：运行中每秒跳动
  （token 随每次 LLM 调用累加实时更新），turn 结束由 message.end 的权威值
  定格，reload 后从 session 的 turns.jsonl 投影恢复（按 bubble 内最后一条
  AIMessage id 精确对齐；老会话静默降级不显示）。
- **工具输出分色**：bash 类输出的 `[exit N]` 行 0=绿/非 0（含 timeout）=红，
  stdout 默认色、stderr 琥珀色文字（不分色块）；标题行 ✓/✗ 跟随退出状态。
- **Runtime 热切换（开发自闭环）**：`make dev-swap`（或会话内 `/dev-swap`）一条命令
  完成「单测 → PyInstaller 重建 → blue-green 预检 → symlink 原子切换 → 壳热重启
  → 构建指纹验证 → 失败自动回滚」——改 runtime 代码不再需要退出重开应用
  （`docs/runtime-hot-swap-design.md`，P0+P1）。TopBar 琥珀 `dev·{指纹}` 黄条标识
  开发 bundle；`make dev-swap-clean` 一键回出厂。
- `/api/health` 现返回 `build`（git 指纹 + channel + repo），壳据此验证热切换。
- 壳新增 sidecar 崩溃自动拉起（watcher：曾在线后掉线且无 restart 在飞 → respawn）。

### Fixed

- goal continuation 两个 flaky：`status` 与 `turns_used` 记账存在写序窗口，
  慢机（CI）上断言读到中间态——等待条件改为两者齐备。

## [0.1.1] - 2026-11-03

### Added

- Full-stack CI: web (lint + typecheck + vitest + build), Rust (fmt + check),
  runtime (pytest with coverage → Codecov) — and green on all three.
- Release automation: `v*` tag → dmg built on a GitHub macOS runner (ad-hoc
  signed), attached to the GitHub Release.
- Frontend test suite bootstrapped (vitest): token formatting, session
  visibility, classname merging, chat waiting-state.
- Community files: CONTRIBUTING, SECURITY policy, PR template, ROADMAP;
  user-facing FAQ / troubleshooting / upgrade / comparison docs.

### Fixed

- Permission denial no longer forks a concurrent agent lane (LangGraph static
  edge + `Command.goto` double-execution; endless-spinner symptom).
- Usage stats expose provider display names instead of raw hash ids.
- Test-suite collection broken since 8d6cb13 (conftest name shadowing) —
  main CI is green again for the first time since then.
- `fmtTokens` doc/impl mismatch (1.25M rendered as 1.3M).

### Changed

- `cargo fmt` baseline for desktop shell + mod-broker (no behavior change).

## [Unreleased]

[0.1.1]: https://github.com/robscc/ginno/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/robscc/ginno/releases/tag/v0.1.0

## [0.1.0] - 2026-10-08

First public release 🎉 (~2.5 months, 220+ commits, daily dogfooding).

### Added

- **Multi-agent chat** — switch agents mid-conversation, shared history,
  per-agent memory.
- **Tools · permissions · skills · MCP · hooks** — Claude Code's shape, built in.
- **Visual Workflow Studio** — versioned DSL compiled to a LangGraph state
  graph, with a live run observer.
- **Local knowledge base** — index your Obsidian vault; answers with citations.
- **Long-horizon goals + TODO sync**; scheduled tasks (30s tick scheduler).
- **Embedded browser + web search** — retrieval with source attribution.
- **Local-first storage** — no DB / no account / no cloud; all state is
  JSON / JSONL / Markdown under `~/.ginno/`.
- **Channels**: DingTalk connector (Stream mode), WeCom.
- Platforms: macOS (Apple Silicon).

[Unreleased]: https://github.com/robscc/ginno/compare/v0.5.3...HEAD
[0.5.3]: https://github.com/robscc/ginno/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/robscc/ginno/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/robscc/ginno/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/robscc/ginno/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/robscc/ginno/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/robscc/ginno/compare/v0.3.3...v0.4.0
[0.1.0]: https://github.com/robscc/ginno/releases/tag/v0.1.0
