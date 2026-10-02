"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";

interface Skill {
  name: string;
  description: string;
  trigger: string;
  tools: string[];
  builtin?: boolean;
}

export function SkillsSettings() {
  const g = useGinno();
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
      g.reloadSkills(); // slash menu of any open chat picks it up immediately
    }
  }
  async function del(n: string) {
    await api.deleteSkill(n);
    load();
    g.reloadSkills();
  }

  async function onImport() {
    const p = importPath.trim();
    if (!p) {
      setImportMsg("Enter a directory path");
      return;
    }
    setImportBusy(true);
    setImportMsg("");
    try {
      const r = await api.importSkillsDir(p, overwrite);
      if (!r.ok) {
        setImportMsg(r.error || "Import failed");
        return;
      }
      const n = (r.imported || []).length;
      const sk = (r.skipped || []).length;
      const er = (r.errors || []).length;
      setImportMsg(
        `Scanned ${r.scanned ?? 0}, imported ${n}` +
          (sk ? `, skipped ${sk}` : "") +
          (er ? `, failed ${er}` : ""),
      );
      if (n > 0) {
        load();
        g.reloadSkills();
      }
    } finally {
      setImportBusy(false);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">Skills</h2>
      <p className="mt-1 text-sm text-muted">One-shot instruction templates (triggered via /&lt;name&gt;). Stored in ~/.ginno/skills/.</p>
      <div className="mt-4 space-y-2">
        {skills.map((s) => (
          <div key={s.name} className="rounded-xl border border-line bg-card p-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm text-violet">/{s.name}</span>
              {s.builtin && (
                <span className="pill border border-violet/40 bg-violet/10 text-violet">built-in</span>
              )}
              <span className="pill border border-line2 text-muted">{s.trigger}</span>
              {s.builtin ? (
                <span className="ml-auto text-xs text-faint" title="Built-in skills cannot be deleted">built-in</span>
              ) : (
                <button onClick={() => del(s.name)} className="ml-auto text-xs text-faint hover:text-red">
                  delete
                </button>
              )}
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
        <div className="mb-2 text-sm font-medium text-txt">Import from a local directory</div>
        <p className="mb-2 text-xs text-muted">
          Point to a skills directory (each subdirectory contains a{" "}
          <code className="text-txt">SKILL.md</code>; lowercase{" "}
          <code className="text-txt">skill.md</code> is also accepted). The whole subdirectory is
          copied (scripts and reference docs included).
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
            Import
          </button>
        </div>
        <label className="mt-2 flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
          Overwrite an existing skill with the same name
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
