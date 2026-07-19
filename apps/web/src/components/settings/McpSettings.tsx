"use client";

import { useEffect, useState } from "react";
import * as api from "@/lib/runtime";

export function McpSettings() {
  const [cfg, setCfg] = useState<string>("");
  const [info, setInfo] = useState<{ servers: string[]; tools: string[] }>({ servers: [], tools: [] });
  const [msg, setMsg] = useState("");

  const load = async () => {
    try {
      const c = await api.getMcpConfig();
      setCfg(JSON.stringify(c, null, 2));
      const i = await api.getMcp();
      setInfo(i);
    } catch {
      /* ignore */
    }
  };
  useEffect(() => {
    load();
  }, []);

  async function save() {
    try {
      const data = JSON.parse(cfg);
      await api.putMcp(data);
      const r = await api.reloadMcp();
      setMsg("saved · servers: " + r.servers.join(", "));
      load();
    } catch (e) {
      setMsg("invalid JSON: " + (e as Error).message);
    }
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">MCP 工具</h2>
      <p className="mt-1 text-sm text-muted">
        已连接 {info.servers.length} server(s)，{info.tools.length} tool(s)。编辑 mcp.json 后保存并重载。
      </p>
      <textarea
        className="field mt-4 font-mono text-xs"
        rows={14}
        value={cfg}
        onChange={(e) => setCfg(e.target.value)}
      />
      <div className="mt-2 flex items-center gap-3">
        <button onClick={save} className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white">
          Save &amp; Reload
        </button>
        {msg && <span className="text-xs text-muted">{msg}</span>}
      </div>
    </div>
  );
}
