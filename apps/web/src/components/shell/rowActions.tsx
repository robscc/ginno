"use client";

// rowActions — session 行动作的单一构造源（2026-11-16）：侧栏行尾扇形菜单
// （RowFanMenu，icon-only 扇叶）与右键上下文菜单（SessionContextMenu，
// icon+label 竖列）共用同一组动作定义——「标为未读」上线时两处菜单要完全
// 同步，双份维护必漂移。构造器是纯函数：所有态（pinned/worker/unread）
// 与回调由调用方注入，tags 面板的打开动作也是注入的 run 回调。

import {
  Bot,
  BotOff,
  GitFork,
  Mail,
  MailCheck,
  Pencil,
  Pin,
  PinOff,
  Tags,
  Trash2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

export interface RowAction {
  key: string;
  /** 菜单文案（右键菜单展示；扇形菜单做 aria-label）。 */
  label: string;
  /** 悬停提示（右键菜单次要行也展示）。 */
  hint: string;
  icon: ReactNode;
  /** 扇叶配色（Tailwind class 段）；右键菜单映射 danger→红。 */
  cls: string;
  danger?: boolean;
  run: () => void;
}

export interface RowActionInputs {
  pinned: boolean;
  onPin: (pinned: boolean) => void;
  onRename: () => void;
  onDelete: () => void;
  worker?: boolean;
  onWorker?: (worker: boolean) => void;
  forkSid?: string;
  onFork?: () => void;
  onTags?: () => void;
  /** 手动未读标注（2026-11-16）：传 onUnread 才有该动作；未读态展示
   *  「标为已读」（复用 seen 语义），已读态展示「标为未读」。 */
  unread?: boolean;
  onUnread?: (unread: boolean) => void;
}

/** 构造 session 行动作列表（顺序 = 两处菜单的展示顺序）。i18n 由内部
 *  useTranslations("shell") 提供——调用方保证在 Client 组件上下文中。 */
export function useRowActions(x: RowActionInputs): RowAction[] {
  const t = useTranslations("shell");
  return [
    ...(x.onFork && x.forkSid
      ? [
          {
            key: "fork",
            label: t("session.fork"),
            hint: t("session.forkHint", { sid: x.forkSid.slice(0, 8) }),
            icon: <GitFork className="h-3.5 w-3.5" />,
            cls: "text-muted hover:text-violet hover:border-violet/40",
            run: x.onFork,
          },
        ]
      : []),
    ...(x.onWorker
      ? [
          {
            // Worker 协议「接活」标注：双态图标（Bot/BotOff，同 Pin/PinOff
            // 模式）——开=amber 常亮（在册员工），关=灰 BotOff。
            key: "worker",
            label: x.worker ? t("session.workerOff") : t("session.workerOn"),
            hint: x.worker ? t("session.workerOffHint") : t("session.workerOnHint"),
            icon: x.worker ? <Bot className="h-4 w-4" /> : <BotOff className="h-4 w-4" />,
            cls: x.worker
              ? "border-amber/50 bg-amber/10 text-amber"
              : "text-faint hover:text-txt hover:border-amber/40",
            run: () => x.onWorker!(!x.worker),
          },
        ]
      : []),
    ...(x.onUnread
      ? [
          {
            key: "unread",
            label: x.unread ? t("session.markRead") : t("session.markUnread"),
            hint: x.unread ? t("session.markReadHint") : t("session.markUnreadHint"),
            icon: x.unread ? (
              <MailCheck className="h-3.5 w-3.5" />
            ) : (
              <Mail className="h-3.5 w-3.5" />
            ),
            cls: "text-muted hover:text-txt hover:border-violet/40",
            run: () => x.onUnread!(!x.unread),
          },
        ]
      : []),
    ...(x.onTags
      ? [
          {
            key: "tags",
            label: t("sessionTags.menu"),
            hint: t("sessionTags.menuHint"),
            icon: <Tags className="h-3.5 w-3.5" />,
            cls: "text-muted hover:text-txt hover:border-violet/40",
            run: x.onTags,
          },
        ]
      : []),
    {
      key: "pin",
      label: x.pinned ? t("session.unpin") : t("session.pin"),
      hint: x.pinned ? t("session.unpinHint") : t("session.pinHint"),
      icon: x.pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />,
      cls: x.pinned ? "text-violet hover:border-violet/50" : "text-muted hover:text-txt",
      run: () => x.onPin(!x.pinned),
    },
    {
      key: "rename",
      label: t("session.rename"),
      hint: t("session.renameHint"),
      icon: <Pencil className="h-3.5 w-3.5" />,
      cls: "text-muted hover:text-txt",
      run: x.onRename,
    },
    {
      key: "delete",
      label: t("session.delete"),
      hint: t("session.delete"),
      icon: <Trash2 className="h-3.5 w-3.5" />,
      cls: "text-muted hover:text-red hover:border-red/40",
      danger: true,
      run: x.onDelete,
    },
  ];
}
