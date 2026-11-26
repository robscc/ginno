import { getSessionHistoryPage, type HistoryPage, type HistoryTarget } from "@/lib/api/sessions";
import type { ChatMsg } from "../streamCore";
import { mapHistory } from "./connect";
import type { EngineCtx } from "./ctx";

export interface HistoryState {
  page?: HistoryPage;
  pending?: Promise<unknown>;
}

/** Serialize reads and their commits per session. A late older page must not
 * prepend onto a replacement checkpoint accepted by a reconnect. */
export function readHistory(
  ctx: EngineCtx,
  sid: string,
  action: "latest" | "older" | "target",
  commit: (rows: ChatMsg[], live: ChatMsg[]) => void,
  target?: string,
): Promise<HistoryTarget | null> {
  let entry = ctx.historyRef.current.get(sid);
  if (!entry) ctx.historyRef.current.set(sid, entry = {});
  const state = entry;
  const work = (state.pending ?? Promise.resolve()).then(async () => {
    if (ctx.historyRef.current.get(sid) !== state) return null;
    const prior = state.page;
    const current = ctx.storeRef.current[sid] ?? [];
    if (target && current.some((row) => row.id === target)) return { id: target, block: null };
    if (action === "older" && !prior?.before) return null;
    const response = await getSessionHistoryPage(sid, {
      before: action === "latest" ? null : prior?.before,
      refresh: action === "latest" ? prior?.tail : null,
      revision: prior?.revision,
      coverage: prior?.coverage,
      target,
    });
    if (ctx.historyRef.current.get(sid) !== state) return null;
    const page = response.page;
    if (page.mode === "locate") return page.target;
    const existing = ctx.storeRef.current[sid] ?? [];
    const noticeId = prior?.notice?.id;
    const saved = existing.filter((row) => row.history && row.id !== noticeId);
    const incoming = mapHistory(ctx, {
      ...response,
      last_error: page.mode === "prepend" ? null : response.last_error,
    }).map((row) => ({ ...row, history: true }));
    const notice = page.notice ? mapHistory(ctx, { messages: [page.notice] })[0] : undefined;
    const live = existing.filter((row) => !row.history && row.id !== page.notice?.id);
    let rows: ChatMsg[] = incoming;
    if (page.mode === "prepend") {
      rows = [...incoming, ...saved];
    } else if (page.mode === "refresh" && prior?.tail) {
      const tailId = prior.tail.slice(0, prior.tail.lastIndexOf(":"));
      const boundary = saved.findIndex((row) => row.id === tailId);
      if (boundary > 0) {
        rows = [...saved.slice(0, boundary), ...incoming];
        page.before = prior.before;
        page.coverage = prior.coverage;
      }
    }
    if (notice) rows = [{ ...notice, history: true }, ...rows];
    if (action !== "latest") {
      const liveId = ctx.liveBySessionRef.current[sid];
      const active = liveId && existing.find((row) => row.id === liveId);
      if (active) {
        const activeIndex = rows.findIndex((row) => row.id === liveId);
        if (activeIndex >= 0) rows[activeIndex] = { ...active, history: true };
        else if (!live.some((row) => row.id === liveId)) live.push({ ...active, history: false });
      }
    }
    state.page = page;
    commit(rows, live);
    return page.target;
  });
  state.pending = work.catch(() => {});
  return work;
}

/** Keep ephemeral rows and a live turn when a reader fetches older history. */
export function appendLiveHistory(rows: ChatMsg[], current: ChatMsg[]): ChatMsg[] {
  const ids = new Set(rows.map((row) => row.id));
  return [...rows, ...current.filter((row) => !ids.has(row.id))];
}
