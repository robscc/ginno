"use client";

import { useTranslations } from "next-intl";
import { BotOff, Check, ChevronDown, Square, Trash2 } from "lucide-react";
import { RowFanMenu } from "@/components/shell/RowFanMenu";
import * as api from "@/lib/runtime";
import { agentHex } from "@/lib/theme";
import { relTime } from "@/lib/utils";
import { AgentIcon } from "@/components/icons";
import type { AgentConfig, SessionMeta, WorkflowRun } from "@/lib/types";
import {
  SUBAGENT_BUCKET_ORDER,
  SUBAGENT_STATUS_META,
  DuckStatusIcon,
  SubagentKindBadges,
  subagentSessionBucket,
  useStatusLabel,
} from "@/components/chat/blocks";

export interface SidebarSessionRowProps {
  s: SessionMeta;
  depth?: number;
  childRows?: SessionMeta[];
  /** 归档区的行整体压暗（仍可点击打开/恢复）。只在归档区顶层行传。 */
  dimmed?: boolean;
  agents: AgentConfig[];
  activeSessionId: string | null;
  onWorkspace: boolean;
  selectMode: boolean;
  selected: Set<string>;
  unreadSessions: Record<string, number>;
  treeCollapsed: Record<string, boolean>;
  editingId: string | null;
  editTitle: string;
  cancelRename: React.MutableRefObject<boolean>;
  subGroupFold: Record<string, boolean>;
  childrenOf: Map<string, SessionMeta[]>;
  runsOf: Map<string, WorkflowRun[]>;
  descendantCount: (id: string) => number;
  onToggleSelect: (id: string) => void;
  /** 打开会话：setActiveSession + 必要时路由回工作区。 */
  onSelectSession: (id: string) => void;
  onStartRename: (s: SessionMeta) => void;
  onEditTitleChange: (value: string) => void;
  /** 退出行内重命名（清 editingId）。 */
  onFinishEditing: () => void;
  onRename: (id: string, title: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  /** Worker 协议「接活」标注（扇形菜单入口）：main 会话行传入，其余不传
   *  （RowFanMenu 缺省不渲染该扇叶）。 */
  onWorker?: (id: string, worker: boolean) => void;
  onRemoveSession: (id: string) => void;
  onSetTreeCollapsed: (id: string, collapsed: boolean) => void;
  /** 子会话分节折叠：显式传当前展开态（默认值由本组件决定）。 */
  onToggleSubGroupFold: (key: string, open: boolean) => void;
  onDelete: (s: SessionMeta) => void;
  renderRunRow: (r: WorkflowRun, depth: number) => React.ReactNode;
}

/**
 * 侧栏会话行（含子会话递归树）。原先是 AppShell 里的 renderSessionRow 闭包，
 * 抽成组件只是为了收窄壳文件；DOM/类名/事件与闭包版逐字一致。
 */
export function SidebarSessionRow(props: SidebarSessionRowProps) {
  const {
    s,
    depth = 0,
    childRows = [],
    dimmed = false,
    agents,
    activeSessionId,
    onWorkspace,
    selectMode,
    selected,
    unreadSessions,
    treeCollapsed,
    editingId,
    editTitle,
    cancelRename,
    subGroupFold,
    childrenOf,
    runsOf,
    descendantCount,
    onToggleSelect,
    onSelectSession,
    onStartRename,
    onEditTitleChange,
    onFinishEditing,
    onRename,
    onPin,
    onWorker,
    onRemoveSession,
    onSetTreeCollapsed,
    onToggleSubGroupFold,
    onDelete,
    renderRunRow,
  } = props;
  const t = useTranslations("shell");
  // 状态枚举的 key 渲染（chat.status.* 命中即译）：子会话行的状态标签经
  // hook 翻译后拼进既有 statusPrefix title 模板。
  const subStatusLabel = useStatusLabel();

  const sel = onWorkspace && s.id === activeSessionId;
  const checked = selectMode && selected.has(s.id);
  const rowAgent = agents.find((a) => a.id === s.agent_id) ?? null;
  const hex = agentHex(rowAgent?.color);
  const editing = editingId === s.id;
  const isSub = s.type === "subagent";
  const subStatus = s.subagent?.status;
  const subMeta = subStatus ? SUBAGENT_STATUS_META[subStatus] : null;
  const subActive = subStatus === "running" || subStatus === "waiting";
  const runRows = runsOf.get(s.id) ?? [];
  const hasKids = childRows.length > 0 || runRows.length > 0;
  const activeKids =
    childRows.filter(
      (c) => c.subagent?.status === "running" || c.subagent?.status === "waiting",
    ).length +
    runRows.filter((r) => r.status === "running" || r.status === "paused").length;
  // 默认展开有子会话的子树——已完成的子会话继续留在列表里可供回看
  // （用户反馈 2026-10-01：全部跑完后子树自动折叠、子会话像消失了一样）；
  // 折叠只在用户点过 chevron 后生效（覆盖持久化在 store）。
  const expanded = treeCollapsed[s.id] === undefined ? hasKids : !treeCollapsed[s.id];
  const openChild = () => {
    // 多选模式下行点击 = 勾选；打开会话要退出多选后再点。
    if (selectMode) {
      onToggleSelect(s.id);
      return;
    }
    onSelectSession(s.id);
  };
  // 递归子会话行：除 s/depth/childRows/dimmed 外沿用本行的全部上下文。
  const renderChildRow = (c: SessionMeta) => (
    <SidebarSessionRow
      key={c.id}
      {...props}
      s={c}
      depth={depth + 1}
      childRows={childrenOf.get(c.id) ?? []}
      dimmed={false}
    />
  );

  return (
    <div>
      <div
        className={`nav-item group ${sel ? "text-txt" : ""} ${
          isSub && !subActive && !sel ? "opacity-60" : ""
        } ${dimmed ? "opacity-60" : ""}`}
        style={{
          ...(sel ? { background: "rgba(99,102,241,0.14)" } : undefined),
          ...(depth > 0 ? { paddingLeft: `${10 + depth * 16}px` } : undefined),
        }}
      >
        {editing ? (
          <input
            autoFocus
            value={editTitle}
            onChange={(e) => onEditTitleChange(e.target.value)}
            onBlur={() => {
              if (cancelRename.current) {
                cancelRename.current = false;
                onFinishEditing();
                return;
              }
              onRename(s.id, editTitle);
              onFinishEditing();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                onRename(s.id, editTitle);
                onFinishEditing();
              } else if (e.key === "Escape") {
                e.preventDefault();
                cancelRename.current = true; // suppress the onBlur commit
                onFinishEditing();
              }
            }}
            onClick={(e) => e.stopPropagation()}
            className="min-w-0 flex-1 rounded border border-line2 bg-base/60 px-1 text-sm text-txt outline-none focus:border-violet"
          />
        ) : (
          <>
            <button
              onClick={openChild}
              onDoubleClick={(e) => {
                if (selectMode) return; // 多选模式下双击不进入重命名
                e.stopPropagation();
                onStartRename(s);
              }}
              className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
            >
              {/* 多选勾选框（P1-4）：纯指示器，点击事件由整行承接。 */}
              {selectMode && (
                <span
                  aria-hidden
                  className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors ${
                    checked ? "border-violet bg-violet text-white" : "border-line2 text-transparent"
                  }`}
                >
                  <Check className="h-3 w-3" />
                </span>
              )}
              {/* 会话行归属：行首图标即 agent 图标（按 agent 色着色），取代原先的
                  agent 名文字标签 —— 归属一眼可见、不占标题宽度。子会话行例外：
                  persona 是继承父会话的（永远是 Dev Agent），标它没有信息量——
                  子会话行仍用状态 emoji + SubagentKindBadges 表达身份。 */}
              {isSub && subMeta ? (
                // 子会话行：状态 emoji 取代会话图标（subagent-design.md §6.1）
                <span
                  className="shrink-0 text-[11px] leading-none"
                  title={t("session.statusPrefix", { status: subStatusLabel(subStatus) })}
                >
                  {subMeta.glyph}
                </span>
              ) : (
                // 归属由行首图标表达：agent 配置了 icon 就显示它（按 agent 色
                // 着色），未配置（或 agent 已删除）统一回退到品牌鸭。
                <span className="shrink-0" style={{ color: hex }} title={rowAgent?.name}>
                  <AgentIcon name={rowAgent?.icon} className="h-4 w-4" />
                </span>
              )}
              <span className="truncate">{s.title || t("session.untitled")}</span>
              {/* Worker 协议「停止接活」角标（worker-protocol-design §2）：仅
                  main 行且显式关掉时渲染——接活是缺省态不标（满屏角标是
                  噪音），关掉是例外态要让状态在行内常驻可见（扇形菜单之外
                  也能一眼看出这个会话不在员工花名册）。 */}
              {s.worker === false && (!s.type || s.type === "main") && (
                <span
                  className="shrink-0 text-faint"
                  title={t("session.workerOffBadge")}
                >
                  <BotOff className="h-3 w-3" />
                </span>
              )}
              {/* fork / 子代理类型徽标（P3 范围 3）：仅子会话行渲染 */}
              {isSub && <SubagentKindBadges sub={s.subagent} />}
              {/* 悬浮速聊窗创建的 quick 会话角标（floating-window-design.md §1.1） */}
              {s.type === "quick" && (
                <span title={t("session.quickBadge")} className="shrink-0 text-[10px] text-yellow">
                  ⚡
                </span>
              )}
              {/* 渠道会话角标（wecom-connector/dingtalk-connector）：桌面只读，
                  对话在 IM 侧进行——标题已带「企业微信/钉钉 ·」前缀，这里只标渠道名。 */}
              {s.type === "channel" && (
                <span
                  title={t("session.channelBadge", {
                    channel: s.channel === "wecom" ? "WeCom" : s.channel === "dingtalk" ? "DingTalk" : "",
                  })}
                  className="shrink-0 rounded border border-line2 px-1 text-[9px] leading-4 text-faint"
                >
                  {s.channel === "wecom" ? "WeCom" : s.channel === "dingtalk" ? "DingTalk" : "IM"}
                </span>
              )}
              {/* 子树还有更深的后代：行尾 +N 尾标（设计 §6.1） */}
              {hasKids && descendantCount(s.id) > childRows.length && (
                <span
                  className="shrink-0 text-[10px] text-faint"
                  title={t("session.moreNested", { count: descendantCount(s.id) - childRows.length })}
                >
                  +{descendantCount(s.id) - childRows.length}
                </span>
              )}
              {/* 未读小圆点：turn 在别处跑完、还没回来读过 → 标题右侧紫点，
                  打开该会话即清除（会话行选中态本身意味着已读）。内存乐观层
                  或服务端装饰（s.unread，session_unread 单一事实源）任一命中。 */}
              <span className="ml-auto flex shrink-0 items-center gap-1.5">
                {(unreadSessions[s.id] !== undefined || s.unread) && !sel && (
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-violet"
                    title={t("session.unreadHint")}
                  />
                )}
                <span className="text-[10px] text-faint">
                  {relTime(s.updated ?? s.created)}
                </span>
              </span>
            </button>
            {/* 父行徽标：运行中子代理/运行数量（设计 §6.1 的「● 2 agents」） */}
            {hasKids && activeKids > 0 && (
              <span
                className="flex shrink-0 items-center gap-1 rounded-full border border-line2 px-1.5 text-[10px] leading-4 text-muted"
                title={t("session.activeKids", { count: activeKids })}
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green" />
                {activeKids}
              </span>
            )}
            {/* 行内动作（展开/置顶/改名/删除）在多选模式下整体隐去，避免
                误触单条删除；子会话的停止/清除同理。 */}
            <span className={`flex shrink-0 items-center gap-0.5 ${selectMode ? "hidden" : ""}`}>
              {hasKids && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    // 存的是「新的折叠态」：当前展开 → 收起（true）。
                    onSetTreeCollapsed(s.id, expanded);
                  }}
                  aria-label={expanded ? t("session.collapse") : t("session.expand")}
                  title={expanded ? t("session.collapse") : t("session.expand")}
                  className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
                >
                  <ChevronDown
                    className={`h-3.5 w-3.5 transition-transform ${expanded ? "" : "-rotate-90"}`}
                  />
                </button>
              )}
              {isSub || s.type === "delegation" ? (
                // 子会话行 hover 入口（P2 共享契约 4）：运行中/等待 → 停止
                // （HTTP 端点，running 走协作式停止、waiting 级联停后代；
                // 状态刷新依赖既有 subagent.status 事件）；结束态 → 清除行
                // （删除该子会话）。delegation 回放同属子会话家族——不进
                // 父会话的 pin/重命名/删除组（pin 对子会话是死按钮，
                // 2026-10-12 用户定稿子会话一律无 pin）。
                subActive ? (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      void api.stopSession(s.id).catch(() => {
                        /* 端点不可达——状态仍以 subagent.status 事件为准 */
                      });
                    }}
                    aria-label={t("session.stop")}
                    title={t("session.stopHint")}
                    className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-yellow group-hover:opacity-100"
                  >
                    <Square className="h-3 w-3" />
                  </button>
                ) : (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemoveSession(s.id);
                    }}
                    aria-label={t("session.clearSub")}
                    title={t("session.clearSubHint")}
                    className="rounded p-1 text-muted opacity-0 transition-opacity hover:bg-card2 hover:text-red group-hover:opacity-100"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )
              ) : (
                <>
                  {/* 置顶/改名/删除收进扇形菜单（RowFanMenu）：行尾只留一个
                      hover 出现的 ⋯，给标题腾宽度；置顶态由「置顶」分组表达。 */}
                  <RowFanMenu
                    pinned={!!s.pinned}
                    onPin={(pinned) => onPin(s.id, pinned)}
                    onRename={() => onStartRename(s)}
                    onDelete={() => onDelete(s)}
                    worker={s.worker !== false}
                    onWorker={
                      // 只给 main 会话行「接活」扇叶（task-typed 天生 worker、
                      // channel 是 IM 实时对话面——都无需标注）
                      onWorker && (!s.type || s.type === "main")
                        ? (w) => onWorker(s.id, w)
                        : undefined
                    }
                  />
                </>
              )}
            </span>
          </>
        )}
      </div>
      {hasKids && expanded && (
        <div className="space-y-0.5">
          {/* 子会话三档分节收纳（2026-10-12）：运行中 → 失败 → 成功，空节不
              渲染；运行中/失败默认展开，成功默认收起（节头计数保证收起仍可
              见——不是 2026-10-01 那种「跑完即消失」）。单子会话不分节。
              孙代递归不受影响：节内行仍走 SidebarSessionRow(depth+1)。 */}
          {childRows.length > 1
            ? SUBAGENT_BUCKET_ORDER.map((bk) => {
                const group = childRows.filter((c) => subagentSessionBucket(c) === bk);
                if (!group.length) return null;
                const key = `${s.id}:${bk}`;
                const open = subGroupFold[key] ?? bk !== "done";
                return (
                  <div key={bk} style={depth > 0 ? { marginLeft: `${depth * 16}px` } : undefined}>
                    <button
                      onClick={() => onToggleSubGroupFold(key, open)}
                      title={t("session.statusPrefix", { status: subStatusLabel(bk) })}
                      className="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-[10px] text-faint transition-colors hover:bg-card2/40 hover:text-muted"
                    >
                      <ChevronDown
                        className={`h-3 w-3 shrink-0 transition-transform ${open ? "" : "-rotate-90"}`}
                      />
                      <DuckStatusIcon status={bk} className="shrink-0" />
                      <span>{subStatusLabel(bk)}</span>
                      <span>· {group.length}</span>
                    </button>
                    {open && group.map(renderChildRow)}
                  </div>
                );
              })
            : childRows.map(renderChildRow)}
          {runRows.map((r) => renderRunRow(r, depth + 1))}
        </div>
      )}
    </div>
  );
}
