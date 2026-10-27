"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import type { Block } from "../blocks/types";
import { useChatT } from "../blocks/i18n";
import { copyText } from "../copyText";

/** blocks → 可复制的纯文本：正文块按序拼接，skill 调用带 `/名字` 前缀。
 * 附件/工具/思考/卡片等非正文块不进剪贴板——复制的是「内容」，不是整段回放。 */
export function blocksToPlainText(blocks: Block[] | undefined | null): string {
  if (!blocks?.length) return "";
  const parts: string[] = [];
  for (const b of blocks) {
    if (b.kind === "text" && b.text.trim()) parts.push(b.text);
    else if (b.kind === "skill") parts.push(`/${b.name}${b.text ? ` ${b.text}` : ""}`);
  }
  return parts.join("\n\n").trim();
}

/** 每个对话气泡的「复制内容」按钮：Copy → Check 1.2s。
 *  没有可复制文本（纯附件/纯工具）时整个按钮不渲染。 */
export function CopyBubbleButton({
  blocks,
  className = "",
}: {
  blocks: Block[] | undefined | null;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const { tc } = useChatT();
  const text = blocksToPlainText(blocks);
  if (!text) return null;
  return (
    <button
      onClick={async () => {
        if (await copyText(text)) {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }
      }}
      title={copied ? tc("copyContent.copied") : tc("copyContent.title")}
      aria-label={tc("copyContent.title")}
      className={`inline-flex h-5 w-5 items-center justify-center rounded transition-colors ${
        copied ? "text-green" : "text-faint hover:text-txt"
      } ${className}`}
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
    </button>
  );
}
