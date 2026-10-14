"use client";

// MentionText — 消息文本里的 @ 提及高亮（2026-10-14 用户需求：@agent 要
// 突出被 @ 的人）。`@agent:名字`（投递/换 persona 的令牌格式）渲染成醒目
// 的成员 chip：紫底 + @ 前缀 + agent 主色圆点；其余提及类别（artifact/
// workflow/memory）用次一级的中性 chip。纯文本部分原样输出。
//
// 只做展示层切分——不解析、不改写、不影响 runtime 的令牌语义。

import { Fragment } from "react";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";

const MENTION_RE = /@(agent|artifact|workflow|memory):([^\s@，。；,.;:!?()（）【】]+)/g;

export function MentionText({ text }: { text: string }) {
  const g = useGinno();
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
          // 突出被 @ 的人：agent 主色点缀 + 紫系 chip。
          const agent = g.agents.find(
            (a) => a.id === name || a.name === name,
          );
          const hex = agentHex(agent?.color);
          return (
            <span
              key={i}
              className="mx-px inline-flex items-center gap-1 rounded-full border border-violet/50 bg-violet/15 px-1.5 py-px align-baseline font-medium text-violet"
            >
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
              @{name}
            </span>
          );
        }
        return (
          <span
            key={i}
            className="mx-px inline-flex items-center rounded-md border border-line bg-card2/70 px-1 py-px align-baseline text-[0.92em] text-muted"
          >
            @{kind}:{name}
          </span>
        );
      })}
    </>
  );
}
