"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";

interface Skill {
  name: string;
  description: string;
  trigger: string;
  tools: string[];
}

export function SkillsSettings() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [name, setName] = useState("");
  const [body, setBody] = useState("");
  const [msg, setMsg] = useState("");
  const [importPath, setImportPath] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [importMsg, setImportMsg] = useState("");
  const [importBusy, setImportBusy] = useState(false);

  const load = () => {
    api.listSkills().then(setSkills).catch(() => {});
  };
  useEffect(() => {
    load();
  }, []);

  async function create() {
    const r = await api.createSkill({ name: name.trim(), body });
    setMsg(r.ok ? "created" : "error: " + (r.error || ""));
    if (r.ok) {
      setName("");
      setBody("");
      load();
    }
  }
  async function del(n: string) {
    await api.deleteSkill(n);
    load();
  }

  async function onImport() {
    const p = importPath.trim();
    if (!p) {
      setImportMsg("请填写目录路径");
      return;
    }
    setImportBusy(true);
    setImportMsg("");
    try {
      const r = await api.importSkillsDir(p, overwrite);
      if (!r.ok) {
        setImportMsg(r.error || "导入失败");
        return;
      }
      const n = (r.imported || []).length;
      const sk = (r.skipped || []).length;
      const er = (r.errors || []).length;
      setImportMsg(
        `扫描 ${r.scanned ?? 0}，导入 ${n}` +
          (sk ? `，跳过 ${sk}` : "") +
          (er ? `，失败 ${er}` : ""),
      );
      if (n > 0) load();
    } finally {
      setImportBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Skills</h2>
      <p className="mt-1 text-sm text-muted">一次性指令模板（/&lt;name&gt; 触发）。存于 ~/.ginno/skills/。</p>
      <div className="mt-4 space-y-2">
        {skills.map((s) => (
          <div key={s.name} className="rounded-xl border border-line bg-card p-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm text-violet">/{s.name}</span>
              <span className="pill border border-line2 text-muted">{s.trigger}</span>
              <button onClick={() => del(s.name)} className="ml-auto text-xs text-faint hover:text-red">
                delete
              </button>
            </div>
            <div className="mt-1 text-xs text-muted">{s.description}</div>
            {s.tools.length > 0 && (
              <div className="mt-1 text-[11px] text-faint">tools: {s.tools.join(", ")}</div>
            )}
          </div>
        ))}
        {skills.length === 0 && <div className="text-xs text-faint">No skills yet.</div>}
      </div>
      <div className="mt-5 rounded-xl border border-line bg-card p-3">
        <div className="mb-2 text-sm font-medium text-txt">从本地目录导入</div>
        <p className="mb-2 text-xs text-muted">
          指向一个 skills 目录（每个子目录含 <code className="text-txt">SKILL.md</code>，兼容小写{" "}
          <code className="text-txt">skill.md</code>）。会复制整个子目录（脚本/参考文档一并导入）。
        </p>
        <div className="flex gap-2">
          <input
            className="field flex-1"
            placeholder="/path/to/.molly/skills"
            value={importPath}
            onChange={(e) => setImportPath(e.target.value)}
          />
          <button
            onClick={onImport}
            disabled={importBusy}
            className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            导入
          </button>
        </div>
        <label className="mt-2 flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
          覆盖已存在的同名 skill
        </label>
        {importMsg && <div className="mt-2 text-xs text-violet">{importMsg}</div>}
      </div>

      <div className="mt-5 rounded-xl border border-line bg-card p-3">
        <div className="mb-2 text-sm font-medium text-txt">New skill</div>
        <input
          className="field mb-2"
          placeholder="name (kebab-case)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <textarea
          className="field mb-2 font-mono text-xs"
          rows={6}
          placeholder={"---\nname: ...\ndescription: ...\ntrigger: user-invocable\n---\n\n# instructions"}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <button onClick={create} className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white">
            Create
          </button>
          {msg && <span className="text-xs text-muted">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
