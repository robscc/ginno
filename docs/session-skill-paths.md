# 会话级 skill_paths

POST `/api/sessions` 在原有必填 `project_slug`、`workspace` 等字段之外接受：

```json
{"project_slug":"default","workspace":"/tmp","skill_paths":["/absolute/repo/skills","/absolute/one-skill"]}
```

`skill_paths` 为可省略的字符串数组，默认 `[]`，不接受 null，最多 20 个输入项。
路径必须为绝对目录（支持 `~` 展开），存在且可读；解析 symlink 后去重。
每个目录可以直接包含 `SKILL.md`，或者包含一级 `<name>/SKILL.md` 子目录；
若自身包含 SKILL.md，则作为单技能目录处理。不递归扫描更深层级。
非法、相对、不存在、非目录或被现有文件权限机制拒绝的路径使请求返回 HTTP 422。

创建响应及磁盘会话 metadata 使用内部字段 `skill_dirs` 保存规范化后的路径；
空列表不写 metadata。运行时会话、AgentState、图重建、重启恢复、系统提示词、
上下文统计、use_skill 都消费该会话的 `skill_dirs`。目录移动或删除后加载时跳过，
不会复制、安装到全局技能目录，也不修改 MCP 配置。未传字段的旧会话沿用原行为。

优先级为 **builtin < global < project < session**。session 数组从左到右覆盖，
同一个集合目录内按路径排序、后者覆盖。覆盖内置技能且未声明 tools 时，继承内置
工具绑定及 sticky；显式声明 tools 时使用自己的绑定，延续现有安全语义。

创建时和每次索引/use_skill 时均按现有 `_DenyPolicy` 校验真实路径。
会话已有 workspace 和挂载目录可提供既有访问权限，`skill_paths` 本身不授权文件访问。
凭据禁区优先于所有挂载；SKILL.md 或技能子目录的 symlink 不得逃出所选目录。
没有进程级 session 技能缓存，不污染其他会话。脚本及相对引用保留在源目录，
use_skill 返回 `Skill directory`；后续文件读取/脚本执行继续接受原有工具权限检查。

系统索引只包含名称和描述，正文由 use_skill 返回。带 skill_dirs 的会话中，
slash 技能请求保持原始 user 文本，不进行正文替换，由模型按索引调用 use_skill。
不传字段的旧会话保留原来的 slash 行为。项目级配置/命令目录接口仍只展示项目与全局
技能；本次不新增 UI 选择器。`trigger: user-invocable` 技能仍受 use_skill 的既有
调用限制，建议会话挂载的技能使用 `both` 或默认 trigger。

本实现为源代码变更；提交、测试不等于部署。独立 review 通过后另行构建发布。
