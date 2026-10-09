"use client";

import { useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Check, ChevronLeft, Loader2, X } from "lucide-react";
import { MCP_TEMPLATES, parseMcpServersJson, type McpServerEntry } from "./mcpShared";

// "sse" 不单独出选项卡（远程卡注明 SSE also supported），但粘贴 sse 配置时会进入此态
// 并按 sse 保存（registry 对 sse/streamable-http 走不同 client，不能混淆）。
type TransportPick = "http" | "stdio" | "sse";

// Add Server 三步向导（原型 v2 / 设计文档 §5.3）：1 选 transport（模板 chips +
// 粘贴 JSON 解析）→ 2 填配置 → 3 Test Connection → Save & Reload。
// Test Connection 在 P0 是纯前端模拟动画（逐行握手 + 模拟工具发现），不落盘、
// 不调后端——真正的试连端点 POST /api/mcp/test 是 P1（设计文档 §4.4），
// 到时候只需替换 runTest 的实现。Save & Reload 才真正 putMcp 合并 + reloadMcp。
export function McpAddWizard({
  cfg,
  onClose,
  onSave,
  onToast,
}: {
  cfg: { mcpServers: Record<string, unknown> };
  onClose: () => void;
  /** 真正保存：putMcp 增量合并 + reloadMcp；成功后由父组件关向导 + toast。 */
  onSave: (name: string, entry: McpServerEntry) => Promise<void>;
  onToast: (msg: string, tone?: "ok" | "err" | "warn") => void;
}) {
  const t = useTranslations("settings.mcp");
  const [step, setStep] = useState(1);
  const [transport, setTransport] = useState<TransportPick>("http");
  const [tpl, setTpl] = useState<string | null>(null);
  const [paste, setPaste] = useState("");
  // step 2 表单
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [hKey, setHKey] = useState("");
  const [hVal, setHVal] = useState("");
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [envKey, setEnvKey] = useState("");
  const [envVal, setEnvVal] = useState("");
  const [timeout, setTimeoutVal] = useState("15.0");
  // step 3 模拟测试
  const [lines, setLines] = useState<string[]>([]);
  const [testing, setTesting] = useState(false);
  const [passed, setPassed] = useState(false);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const isStdio = transport === "stdio";

  const applyTemplate = (key: string) => {
    const p = MCP_TEMPLATES.find((x) => x.key === key);
    if (!p) return;
    setTpl(key);
    setTransport(p.transport);
    setName(p.name);
    if (p.transport === "stdio") {
      setCommand(p.command ?? "");
      setArgsText(p.args ?? "");
      setEnvKey(p.env ?? "");
      setEnvVal("");
    } else {
      setUrl(p.url ?? "");
    }
    setStep(2);
  };

  // 粘贴 JSON 解析：取第一个 server 预填表单（type/transport 拼写归一化在
  // parseMcpServersJson 内做预览；保存后 registry 还会再归一化一次）
  const parsePaste = () => {
    try {
      const parsed = parseMcpServersJson(paste);
      const first = parsed[0];
      const e = first.entry;
      const tr = String(e.transport ?? "").toLowerCase();
      setTransport(tr === "stdio" ? "stdio" : tr === "sse" ? "sse" : "http");
      setName(first.name);
      if ("command" in e) {
        setCommand(String(e.command ?? ""));
        setArgsText(Array.isArray(e.args) ? (e.args as unknown[]).map(String).join("\n") : "");
        const env = (e.env ?? {}) as Record<string, unknown>;
        const k = Object.keys(env)[0] ?? "";
        setEnvKey(k);
        setEnvVal(k ? String(env[k] ?? "") : "");
      } else {
        setUrl(String(e.url ?? ""));
      }
      setStep(2);
      onToast(t("wzParseOk", { count: parsed.length, name: first.name }), "ok");
    } catch {
      onToast(t("wzParseFail"), "err");
    }
  };

  const buildEntry = (): McpServerEntry => {
    const entry: McpServerEntry =
      transport === "stdio"
        ? {
            transport: "stdio",
            command: command.trim() || "npx",
            args: argsText
              .split("\n")
              .map((s) => s.trim())
              .filter(Boolean),
          }
        : {
            transport: transport === "sse" ? "sse" : "streamable-http",
            url: url.trim(),
          };
    if (!isStdio && hKey.trim()) entry.headers = { [hKey.trim()]: hVal };
    if (isStdio && envKey.trim()) entry.env = { [envKey.trim()]: envVal };
    const to = Number(timeout);
    if (Number.isFinite(to) && to > 0) entry.connect_timeout = to;
    return entry;
  };

  // P0 模拟握手动画：逐行 ✓（~450ms 间隔，对应原型 runTest 的节奏），全部走完
  // 即视为通过并展示模拟的工具发现预览（真实发现数要等保存后从 /api/mcp 读取）。
  const runTest = () => {
    if (!name.trim()) {
      onToast(t("cfgNameRequired"), "err");
      return;
    }
    if (!isStdio && !url.trim()) {
      onToast(t("cfgNameRequired"), "err");
      return;
    }
    timers.current.forEach(clearTimeout);
    timers.current = [];
    setTesting(true);
    setPassed(false);
    setLines([]);
    const seq = isStdio
      ? [t("wzTestSpawn", { cmd: command.trim() || "npx" }), t("wzTestList"), t("wzTestValidate")]
      : [t("wzTestHandshake", { url: url.trim() || "…" }), t("wzTestList"), t("wzTestValidate")];
    seq.forEach((l, i) => {
      timers.current.push(
        setTimeout(() => {
          setLines((prev) => [...prev, l]);
          if (i === seq.length - 1) {
            setTesting(false);
            setPassed(true);
          }
        }, 450 * (i + 1)),
      );
    });
  };

  const overwrite = !!cfg.mcpServers && name.trim() in cfg.mcpServers;
  // 模拟预览的图名（P0 占位；P1 换 POST /api/mcp/test 的真实 toolDetails）
  const simTools = ["search", "get_detail", "create_item"].map((s) => `mcp_${name.trim() || "server"}_${s}`);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-5">
      <div className="max-h-[88vh] w-[min(640px,94vw)] overflow-auto rounded-xl border border-line2 bg-panel shadow-2xl">
        {/* 头部：标题 + 步骤指示 */}
        <div className="flex items-center gap-3 border-b border-line px-5 py-3.5">
          <h3 className="text-[15px] font-semibold text-txt">{t("wzTitle")}</h3>
          <span className="ml-auto flex items-center gap-1.5 text-xs text-faint">
            {[t("wzStepTransport"), t("wzStepConfigure"), t("wzStepTest")].map((n, i) => (
              <span key={n} className={i + 1 === step ? "font-semibold text-txt" : undefined}>
                {i > 0 && <span className="mr-1.5">·</span>}
                {i + 1 === step ? <b>{`${i + 1} ${n}`}</b> : `${i + 1} ${n}`}
              </span>
            ))}
          </span>
          <button
            type="button"
            aria-label={t("wzClose")}
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-faint hover:bg-card2 hover:text-txt"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="p-5">
          {/* ---- Step 1: transport ---- */}
          {step === 1 && (
            <div>
              <div className="mb-1 grid grid-cols-[1.2fr_1fr] gap-2.5">
                <TransportOption
                  title={t("wzOptRemoteTitle")}
                  desc={t("wzOptRemoteDesc")}
                  selected={transport === "http"}
                  onClick={() => setTransport("http")}
                  icon={<GlobeIcon />}
                />
                <TransportOption
                  title={t("wzOptStdioTitle")}
                  desc={t("wzOptStdioDesc")}
                  selected={transport === "stdio"}
                  onClick={() => setTransport("stdio")}
                  icon={<TerminalIcon />}
                />
              </div>
              <div className="mb-2 mt-3.5 text-[11.5px] font-semibold uppercase tracking-wider text-faint">
                {t("wzTplLabel")}
              </div>
              <div className="flex flex-wrap gap-2">
                {MCP_TEMPLATES.map((p) => (
                  <button
                    key={p.key}
                    type="button"
                    onClick={() => applyTemplate(p.key)}
                    className={`rounded-full border px-3 py-1.5 text-[12.5px] transition-colors ${
                      tpl === p.key
                        ? "border-indigo bg-indigo/10 text-txt"
                        : "border-line2 bg-card text-muted hover:text-txt"
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              <details className="mt-3.5 rounded-lg border border-line px-3.5 py-2.5">
                <summary className="cursor-pointer text-[12.5px] text-muted select-none">
                  {t("wzPasteLabel")}
                </summary>
                <textarea
                  className="field mt-2.5 font-mono text-xs"
                  rows={7}
                  spellCheck={false}
                  value={paste}
                  onChange={(e) => setPaste(e.target.value)}
                />
                <div className="mt-2">
                  <button
                    type="button"
                    onClick={parsePaste}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-line2 px-3 py-1.5 text-[13px] font-medium text-txt transition-colors hover:bg-card2"
                  >
                    {t("wzParse")}
                  </button>
                </div>
              </details>
              <div className="mt-4 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setStep(2)}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-indigo px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-indigo2"
                >
                  {t("wzContinue")} →
                </button>
              </div>
            </div>
          )}

          {/* ---- Step 2: configure ---- */}
          {step === 2 && (
            <div>
              <div className="max-w-[580px]">
                <label className="field-label">{t("cfgName")}</label>
                <input
                  className="field"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t("cfgNamePh")}
                  autoFocus
                />
                {overwrite && (
                  <p className="mt-1 text-[11.5px] text-yellow">{t("wzOverwrite", { name: name.trim() })}</p>
                )}
                {isStdio ? (
                  <>
                    <label className="field-label mt-3.5">{t("cfgCommand")}</label>
                    <input
                      className="field font-mono text-[13px]"
                      value={command}
                      onChange={(e) => setCommand(e.target.value)}
                      placeholder="npx"
                    />
                    <label className="field-label mt-3.5">{t("cfgArgs")}</label>
                    <textarea
                      className="field font-mono text-[13px]"
                      rows={2}
                      value={argsText}
                      onChange={(e) => setArgsText(e.target.value)}
                    />
                    <label className="field-label mt-3.5">{t("cfgEnv")}</label>
                    <div className="flex items-center gap-2">
                      <input
                        className="field w-[170px] font-mono text-[13px]"
                        value={envKey}
                        onChange={(e) => setEnvKey(e.target.value)}
                        placeholder="GITHUB_TOKEN"
                      />
                      <input
                        className="field flex-1 font-mono text-[13px]"
                        type="password"
                        value={envVal}
                        onChange={(e) => setEnvVal(e.target.value)}
                        placeholder={t("wzValue")}
                      />
                    </div>
                  </>
                ) : (
                  <>
                    <label className="field-label mt-3.5">{t("cfgUrl")}</label>
                    <input
                      className="field font-mono text-[13px]"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      placeholder="https://mcp-gw.dingtalk.com/server/...?key=..."
                    />
                    <label className="field-label mt-3.5">{t("cfgHeaders")}</label>
                    <div className="flex items-center gap-2">
                      <input
                        className="field w-[170px] font-mono text-[13px]"
                        value={hKey}
                        onChange={(e) => setHKey(e.target.value)}
                        placeholder="Authorization"
                      />
                      <input
                        className="field flex-1 font-mono text-[13px]"
                        type="password"
                        value={hVal}
                        onChange={(e) => setHVal(e.target.value)}
                        placeholder={t("wzValue")}
                      />
                    </div>
                    <p className="mt-1.5 text-[11.5px] leading-relaxed text-faint">{t("cfgHeadersNote")}</p>
                  </>
                )}
                <label className="field-label mt-3.5">{t("cfgTimeout")}</label>
                <input
                  className="field w-[160px] font-mono text-[13px]"
                  value={timeout}
                  onChange={(e) => setTimeoutVal(e.target.value)}
                />
              </div>
              <div className="mt-4 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setStep(1)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-line2 px-3 py-1.5 text-[13px] font-medium text-txt transition-colors hover:bg-card2"
                >
                  <ChevronLeft className="h-3.5 w-3.5" /> {t("wzBack")}
                </button>
                <button
                  type="button"
                  onClick={runTest}
                  disabled={testing}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-indigo px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-indigo2 disabled:opacity-45"
                >
                  {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                  {testing ? t("wzTesting") : t("testConnection")}
                </button>
              </div>
            </div>
          )}

          {/* ---- Step 3: 模拟测试 ---- */}
          {step === 3 && (
            <div>
              <div className="mb-3.5 min-h-[90px] rounded-lg border border-line bg-[rgb(var(--code-bg))] px-3.5 py-3 font-mono text-xs leading-[1.9] text-muted">
                {lines.map((l, i) => (
                  <div key={i}>
                    <span className="mr-1.5 text-green">✓</span>
                    {l}
                    {testing && i === lines.length - 1 && (
                      <Loader2 className="ml-1.5 inline h-3 w-3 animate-spin" />
                    )}
                  </div>
                ))}
              </div>
              {passed && (
                <div>
                  <div className="mb-3 flex items-start gap-2.5 rounded-lg border border-green/30 bg-green/[0.07] px-3.5 py-3">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-green" />
                    <div>
                      <b className="block text-[13.5px] text-txt">
                        {t("wzTestOkTitle", { name: name.trim() || "server", count: 21 })}
                      </b>
                      <span className="text-xs text-muted">
                        {t("wzTestOkSub", { transport: isStdio ? "stdio" : "streamable-http", timeout })}
                      </span>
                    </div>
                  </div>
                  <div className="mb-2 mt-3.5 text-[11.5px] font-semibold uppercase tracking-wider text-faint">
                    {t("wzToolsDiscovered")}
                  </div>
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {simTools.map((n) => (
                      <span
                        key={n}
                        className="rounded-md border border-line2 px-2 py-0.5 font-mono text-[11px] text-muted"
                      >
                        {n}
                      </span>
                    ))}
                    <span className="rounded-md border border-line2 px-2 py-0.5 font-mono text-[11px] text-muted">
                      {t("wzToolsMore", { count: 18 })}
                    </span>
                  </div>
                  <p className="text-xs text-yellow">⚠ {t("wzToolsWarn")}</p>
                </div>
              )}
              <div className="mt-4 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setStep(2)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-line2 px-3 py-1.5 text-[13px] font-medium text-txt transition-colors hover:bg-card2"
                >
                  <ChevronLeft className="h-3.5 w-3.5" /> {t("wzBack")}
                </button>
                <button
                  type="button"
                  disabled={!passed}
                  onClick={async () => {
                    await onSave(name.trim(), buildEntry());
                  }}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-indigo px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-indigo2 disabled:opacity-45"
                >
                  {t("saveReload")}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function TransportOption({
  title,
  desc,
  selected,
  onClick,
  icon,
}: {
  title: string;
  desc: string;
  selected: boolean;
  onClick: () => void;
  icon: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-[10px] border p-3.5 text-left transition-colors ${
        selected ? "border-indigo bg-indigo/[0.06]" : "border-line hover:border-line2"
      }`}
    >
      <span className="mb-2 block text-indigo">{icon}</span>
      <b className="mb-1 block text-[13.5px] text-txt">{title}</b>
      <span className="block text-xs leading-[1.45] text-muted">{desc}</span>
    </button>
  );
}

function GlobeIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z" />
    </svg>
  );
}

function TerminalIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m5 8 4 4-4 4M11 17h8" />
    </svg>
  );
}
