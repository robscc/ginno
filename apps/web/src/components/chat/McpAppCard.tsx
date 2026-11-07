"use client";

/**
 * MCP App 卡（docs/mcp-apps-design.md §2 P0 官方形态）：工具命中
 * tools-with-ui 清单时，在普通工具气泡上方附加的沙箱 iframe 交互卡。
 *
 * - GET /api/mcp/ui 取 ui:// resource（404 → null，由调用方回退普通渲染）。
 * - 懒渲染：进入视口（rootMargin 400px 预取）才建 iframe——srcDoc 大 HTML
 *   一次性解析很贵（参照 MermaidBlock 的 IntersectionObserver 惯例）。
 * - 沙箱底线：sandbox 恒为 "allow-scripts"（绝不加 allow-same-origin）；
 *   permissions 声明时按需把 token 附加到 iframe allow 并显示提示条
 *   （P0 原样透传，逐 token 映射在 P1 transcript-server 验证时完善）。
 * - 双向消息走 McpAppBridge（lib/mcpAppBridge.ts）；初始工具结果在
 *   iframe onLoad 后经 ui/data 推入。
 */

import { useEffect, useRef, useState } from "react";
import { Puzzle } from "lucide-react";
import { getMcpToolUi, mcpAppCall, type McpToolUiPayload } from "@/lib/runtime";
import { McpAppBridge } from "@/lib/mcpAppBridge";
import { useChatT } from "./blocks/i18n";

const IFRAME_HEIGHT = 360; // P0 无 resize 协议：固定高度，容器可折叠
const CONTAINER_MAX = 420;

export function McpAppCard({
  server,
  tool,
  data,
}: {
  server: string;
  /** graph 面向全名（清单键） */
  tool: string;
  /** 触发本次渲染的工具结果文本（app 的初始数据） */
  data?: string;
}) {
  const [ui, setUi] = useState<McpToolUiPayload | null | "loading">("loading");
  const [inView, setInView] = useState(false);
  const [open, setOpen] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const bridgeRef = useRef<McpAppBridge | null>(null);
  // onLoad 与 bridge effect 的次序无保证：ref 记「已 load」，晚到者补推初始数据
  const loadedRef = useRef(false);
  const { tc } = useChatT();

  // ui:// resource：404/失败都回 null（调用方回退普通气泡，不渲染错误卡）
  useEffect(() => {
    let alive = true;
    getMcpToolUi(server, tool)
      .then((payload) => {
        if (alive) setUi(payload);
      })
      .catch(() => {
        if (alive) setUi(null);
      });
    return () => {
      alive = false;
    };
  }, [server, tool]);

  // 懒渲染：进入视口才建 iframe（MermaidBlock 惯例 + document.hidden 兜底）
  useEffect(() => {
    if (inView || !ui || ui === "loading") return;
    const el = boxRef.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          io.disconnect();
        }
      },
      { rootMargin: "400px 0px" },
    );
    io.observe(el);
    const t = document.hidden ? setTimeout(() => setInView(true), 800) : null;
    return () => {
      io.disconnect();
      if (t !== null) clearTimeout(t);
    };
  }, [inView, ui]);

  // bridge 生命周期：iframe 挂载（inView 且有 html）后绑定；卸载即 dispose
  useEffect(() => {
    if (!inView || !ui || ui === "loading") return;
    const iframe = iframeRef.current;
    if (!iframe || !iframe.contentWindow) return;
    const bridge = new McpAppBridge({
      targetWindow: iframe.contentWindow,
      postToApp: (msg) => iframe.contentWindow?.postMessage(msg, "*"),
      callMcp: (rawTool, args) => mcpAppCall(server, rawTool, args),
    });
    bridgeRef.current = bridge;
    const onMessage = (ev: MessageEvent) => bridge.handleMessage(ev);
    window.addEventListener("message", onMessage);
    // 竞态兜底：iframe onLoad 先于本 effect 时补推一次初始数据
    if (loadedRef.current) {
      bridge.pushData({ tool, content: [data ?? ""] });
    }
    return () => {
      window.removeEventListener("message", onMessage);
      bridge.dispose();
      bridgeRef.current = null;
    };
  }, [inView, ui, server, tool, data]);

  if (!ui || ui === "loading") return null;

  const perms = (ui.permissions ?? []).filter((p) => typeof p === "string" && p);

  return (
    <div
      ref={boxRef}
      className="my-1.5 overflow-hidden rounded-md border border-violet/40 bg-violet/[0.06]"
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-left transition-colors hover:bg-violet/10"
        title={ui.tool}
      >
        <Puzzle className="h-3.5 w-3.5 shrink-0 text-violet" />
        <span className="truncate text-xs font-medium text-violet">
          {tc("mcpApp.app")} · {ui.server}
        </span>
        <span className="shrink-0 rounded-full border border-violet/40 px-1.5 text-[10px] leading-4 text-violet/80">
          {tc("mcpApp.sandbox")}
        </span>
        <span className="ml-auto shrink-0 text-[10px] text-faint">
          {open ? tc("tools.collapse") : tc("tools.expand")}
        </span>
      </button>
      {perms.length > 0 && (
        <div className="border-t border-violet/15 px-2.5 py-1 text-[10px] leading-4 text-amber-600/90 dark:text-amber-400/80">
          {tc("mcpApp.permissions", { perms: perms.join(", ") })}
        </div>
      )}
      {open && inView && (
        <iframe
          ref={iframeRef}
          title={`${ui.server} app`}
          sandbox="allow-scripts"
          srcDoc={ui.html}
          allow={perms.length ? perms.join("; ") : undefined}
          className="w-full border-0"
          style={{ height: IFRAME_HEIGHT, maxHeight: CONTAINER_MAX }}
          onLoad={() => {
            // 初始工具结果：app 加载完成即推送（后续更新经 bridge 的 ui/data）
            loadedRef.current = true;
            bridgeRef.current?.pushData({ tool: ui.tool, content: [data ?? ""] });
          }}
        />
      )}
    </div>
  );
}
