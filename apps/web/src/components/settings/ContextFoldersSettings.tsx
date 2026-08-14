"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import type { FolderEntry, FolderProbe } from "@/lib/types";
import { FolderInput, Search, Plus, Trash2 } from "lucide-react";

function AccessToggle({
  access,
  onChange,
}: {
  access: "ro" | "rw";
  onChange: (a: "ro" | "rw") => void;
}) {
  return (
    <button
      onClick={() => onChange(access === "rw" ? "ro" : "rw")}
      title="点击切换访问级：rw 可读写 / ro 只读（工具层硬约束）"
      className="rounded border border-line2 px-1.5 py-0.5 font-mono text-[11px] transition-colors"
      style={{
        color: access === "rw" ? "#4ade80" : "#fbbf24",
        background: access === "rw" ? "#22c55e14" : "#f59e0b14",
      }}
    >
      {access === "rw" ? "读写" : "只读"}
    </button>
  );
}

export function ContextFoldersSettings() {
  const [folders, setFolders] = useState<FolderEntry[]>([]);
  const [path, setPath] = useState("");
  const [access, setAccess] = useState<"ro" | "rw">("rw");
  const [loadRules, setLoadRules] = useState(true);
  const [probe, setProbe] = useState<FolderProbe | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    api
      .listFolders()
      .then((r) => setFolders(r.folders || []))
      .catch(() => {});
  }, []);
  useEffect(reload, [reload]);

  async function doProbe() {
    setProbe(null);
    setMsg("");
    if (!path.trim()) {
      setMsg("请先填写目录路径");
      return;
    }
    setProbe(await api.probeFolder(path.trim()));
  }

  async function add() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.createFolder({ path: path.trim(), access, load_rules: loadRules });
      if (!r.ok) {
        setMsg(r.error || "添加失败");
        return;
      }
      setMsg(`已加入目录库：${r.folder?.name}`);
      setPath("");
      setProbe(null);
      reload();
    } finally {
      setBusy(false);
    }
  }

  async function patch(id: string, p: Partial<FolderEntry>) {
    await api.updateFolder(id, p);
    reload();
  }

  async function remove(f: FolderEntry) {
    if (!window.confirm(`从目录库移除「${f.name}」？已挂载它的会话会显示为缺失。`)) return;
    await api.deleteFolder(f.id);
    reload();
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-txt">
        <FolderInput className="h-5 w-5 text-violet" /> 上下文目录
      </h2>
      <p className="mt-1 text-sm text-muted">
        把本地目录（代码仓库、笔记、文档）注册进目录库，然后在会话中挂载（TopBar 的 📁 菜单或{" "}
        <code className="text-txt">/mount</code> 命令）。挂载后 Agent
        可以直接读写其中的文件；目录内的 <code className="text-txt">AGENTS.md</code> /{" "}
        <code className="text-txt">GINNO.md</code> 会作为该目录的规则注入。
      </p>

      {/* ---- add form ---- */}
      <div className="mt-6 rounded-xl border border-line bg-card p-4">
        <div className="text-sm font-medium text-txt">添加目录</div>
        <div className="mt-3 flex gap-2">
          <input
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              setProbe(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && doProbe()}
            placeholder="绝对路径，如 ~/workspace/my-repo"
            className="field flex-1"
          />
          <button
            onClick={doProbe}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            <Search className="h-3.5 w-3.5" /> 检测
          </button>
        </div>

        {probe && (
          <div className="mt-3 rounded-lg border border-line2 bg-card2 px-3 py-2 text-xs">
            {probe.ok ? (
              <div className="space-y-1 text-muted">
                <div>
                  <span className="text-txt">{probe.path}</span> · {probe.file_count}
                  {probe.file_count_truncated ? "+" : ""} 个文件
                  {probe.has_git ? " · git 仓库" : ""}
                </div>
                <div>
                  {probe.rule_file ? (
                    <span style={{ color: "#4ade80" }}>检测到 {probe.rule_file}（将作为规则注入）</span>
                  ) : (
                    <span className="text-faint">未发现 AGENTS.md / GINNO.md</span>
                  )}
                  {probe.already_registered && <span style={{ color: "#fbbf24" }}> · 已在目录库中（将更新）</span>}
                </div>
              </div>
            ) : (
              <div style={{ color: "#f87171" }}>{probe.error}</div>
            )}
          </div>
        )}

        <div className="mt-3 flex items-center gap-4 text-sm text-muted">
          <label className="flex items-center gap-2">
            访问级
            <select
              value={access}
              onChange={(e) => setAccess(e.target.value as "ro" | "rw")}
              className="field w-auto py-1"
            >
              <option value="rw">读写（rw）</option>
              <option value="ro">只读（ro）</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={loadRules}
              onChange={(e) => setLoadRules(e.target.checked)}
            />
            加载其规则文件（AGENTS.md / GINNO.md）
          </label>
          <button
            onClick={add}
            disabled={busy || !path.trim()}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" /> 加入目录库
          </button>
        </div>
        {msg && <div className="mt-2 text-xs text-muted">{msg}</div>}
      </div>

      {/* ---- library list ---- */}
      <div className="mt-6">
        <div className="mb-2 text-sm font-medium text-txt">目录库（{folders.length}）</div>
        {folders.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line2 px-4 py-8 text-center text-sm text-faint">
            还没有注册任何目录。添加后即可在会话中挂载。
          </div>
        ) : (
          <div className="space-y-2">
            {folders.map((f) => (
              <div
                key={f.id}
                className="flex items-center gap-3 rounded-xl border border-line bg-card px-4 py-3"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-txt">{f.name}</span>
                    <AccessToggle access={f.access} onChange={(a) => patch(f.id, { access: a })} />
                  </div>
                  <div className="truncate font-mono text-xs text-faint" title={f.path}>
                    {f.path}
                  </div>
                </div>
                <label
                  className="flex shrink-0 items-center gap-1.5 text-xs text-muted"
                  title="是否将该目录的 AGENTS.md / GINNO.md 注入挂载它的会话"
                >
                  <input
                    type="checkbox"
                    checked={f.load_rules}
                    onChange={(e) => patch(f.id, { load_rules: e.target.checked })}
                  />
                  规则
                </label>
                <button
                  onClick={() => remove(f)}
                  className="shrink-0 rounded-lg p-1.5 text-faint hover:bg-card2 hover:text-red-400"
                  title="从目录库移除"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-xs text-faint">
          安全边界：挂载只授予文件访问权 —— 目录内的 settings / hooks / skills 永不加载（access ≠
          config）；只读级是工具层硬约束，与特权模式无关。
        </p>
      </div>
    </div>
  );
}
