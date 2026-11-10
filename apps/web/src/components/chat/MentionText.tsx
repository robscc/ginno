"use client";

// MentionText — 消息文本里的 @ 提及 chip 化（2026-11-10 用户定稿）：
// 不再显示裸 @ token，统一「图标 + 名字」——
//   @agent:名     → Bot 图标（agent 主色）+ 名（投递/换 persona 令牌）
//   @artifact:名  → 文件类型图标（vscode-icons，按扩展名）+ 文件名
//   @session:名   → MessageSquare + 会话名
//   @result:名    → BarChart3 + 标题
//   workflow/memory（旧 token）→ 对应图标 + 名，保持兼容。
// 纯文本部分原样输出。只做展示层切分——不解析、不改写、不影响 runtime
// 的令牌语义（发送侧协议仍是 `@kind:label `，见 ChatStream.pickItem）。

import { Fragment } from "react";
import { BarChart3, Bot, Brain, MessageSquare, Workflow } from "lucide-react";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";
import { FileTypeIcon } from "./blocks/FileTypeIcon";

// label 允许 ASCII 词间空格（"Dev Agent"）；CJK 与标点自然终止，避免
// 吞掉后续中文句子。
const MENTION_RE =
  /@(agent|artifact|session|result|workflow|memory):([\w.\-\/]+(?: [A-Za-z0-9_.\-\/]+)*)/g;

function MentionIcon({ kind, name }: { kind: string; name: string }) {
  const g = useGinno();
  const cls = "h-3 w-3 shrink-0";
  switch (kind) {
    case "agent": {
      // 突出被 @ 的人：agent 主色 Bot 图标（2026-10-14 定稿的主色点缀延续）。
      const agent = g.agents.find((a) => a.id === name || a.name === name);
      return <Bot className={cls} style={{ color: agentHex(agent?.color) }} />;
    }
    case "artifact":
      // 全类型文件图标（public/fileicons，PR #57 资产复用）。
      return <FileTypeIcon name={name} className={cls} />;
    case "session":
      return <MessageSquare className={cls} />;
    case "result":
      return <BarChart3 className={cls} />;
    case "workflow":
      return <Workflow className={cls} />;
    default:
      return <Brain className={cls} />;
  }
}

export function MentionText({ text }: { text: string }) {
  const out: Array<string | { kind: string; name: string }> = [];
  let last = 0;
  for (const m of text.matchAll(MENTION_RE)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push(text.slice(last, idx));
    out.push({ kind: m[1], name: m[2] });
    last = idx + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  if (!out.some((p) => typeof p !== "string")) return <>{text}</>;

  return (
    <>
      {out.map((part, i) => {
        if (typeof part === "string") return <Fragment key={i}>{part}</Fragment>;
        const { kind, name } = part;
        if (kind === "agent") {
          return (
            <span
              key={i}
              className="mx-px inline-flex max-w-[280px] items-center gap-1 rounded-full border border-violet/50 bg-violet/15 px-1.5 py-px align-baseline font-medium text-violet"
            >
              <MentionIcon kind={kind} name={name} />
              <span className="truncate">{name}</span>
            </span>
          );
        }
        return (
          <span
            key={i}
            className="mx-px inline-flex max-w-[280px] items-center gap-1 rounded-md border border-line bg-card2/70 px-1 py-px align-baseline text-[0.92em] text-muted"
          >
            <MentionIcon kind={kind} name={name} />
            <span className="truncate">{name}</span>
          </span>
        );
      })}
    </>
  );
}
