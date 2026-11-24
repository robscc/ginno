const ginno = "http://127.0.0.1:8787";
export async function onRequest(body, ctx) {
  if (ctx.agent !== "ginno" || !["local", "magpie-key"].includes(ctx.origin) ||
      !/^[a-f0-9]{32}$/.test(ctx.session) || body.client_metadata !== undefined) return;
  const response = await fetch(ginno + "/api/sessions/" + ctx.session, {signal: ctx.signal, redirect: "error"});
  if (!response.ok) return;
  const meta = await response.json();
  if (meta.id === ctx.session && meta.type === "subagent") {
    return {...body, client_metadata: {thread_source: "collab_spawn", ginno_label_test: "1"}};
  }
}
