import { getSessionHistoryPage, type HistoryPage, type HistoryResult, type HistoryTarget } from "@/lib/api/sessions";
import type { ChatMsg } from "../streamCore";
import { mapHistory } from "./connect";
import type { EngineCtx } from "./ctx";

type HistoryAction = "latest" | "older" | "target";

type HistoryFailure =
  | { kind: "http"; status: number }
  | { kind: "connection" }
  | { kind: "invalidResponse" };

type InitialHistoryStatus = { kind: "loading" } | { kind: "failed"; error: HistoryFailure };
export type HistoryLoadStatus = InitialHistoryStatus | { kind: "ready" };

export const HISTORY_LOADING: InitialHistoryStatus = { kind: "loading" };
export const HISTORY_READY: HistoryLoadStatus = { kind: "ready" };

export function historyFailure(error: unknown): HistoryFailure {
  if (error instanceof Error && "ginnoStatus" in error && typeof error.ginnoStatus === "number") {
    return { kind: "http", status: error.ginnoStatus };
  }
  return { kind: error instanceof TypeError ? "connection" : "invalidResponse" };
}

export interface HistoryState {
  page?: HistoryPage;
  pending?: Promise<unknown>;
  initial?: InitialHistoryStatus;
}

export interface HistoryReadOptions {
  indexRevision?: string;
}

function isHistoryRow(row: HistoryResult["messages"][number]): boolean {
  return Boolean(row && typeof row.id === "string" && ["user", "assistant", "system"].includes(row.role) &&
    Array.isArray(row.blocks) && row.blocks.every((block) => block && typeof block.kind === "string"));
}

function validateHistory(response: HistoryResult & { page: HistoryPage }, action: HistoryAction, prior?: HistoryPage) {
  const page = response?.page;
  const mode = page?.mode;
  const validMode = mode === "replace" || (mode === "refresh" && action === "latest" && prior?.tail) ||
    (mode === "prepend" && action !== "latest" && prior?.before) || (mode === "locate" && action === "target");
  if (response?.ok !== true || !Array.isArray(response.messages) || !response.messages.every(isHistoryRow) ||
    !page || !validMode || typeof page.revision !== "string" ||
    ![page.tail, page.before, page.coverage].every((cursor) => cursor === null || typeof cursor === "string") ||
    !(page.notice === null || isHistoryRow(page.notice)) ||
    !(page.target === null || (page.target && typeof page.target.id === "string" &&
      (page.target.block === null || Number.isInteger(page.target.block))))) {
    throw new Error("Invalid session history response");
  }
}

/** Serialize reads and their commits per session. A late older page must not
 * prepend onto a replacement checkpoint accepted by a reconnect. */
export function readHistory(
  ctx: EngineCtx,
  sid: string,
  action: HistoryAction,
  commit: (rows: ChatMsg[], live: ChatMsg[]) => void,
  target?: string,
  { indexRevision }: HistoryReadOptions = {},
): Promise<HistoryTarget | null> {
  let entry = ctx.historyRef.current.get(sid);
  if (!entry) ctx.historyRef.current.set(sid, entry = {});
  const state = entry;
  const work = (state.pending ?? Promise.resolve()).then(async () => {
    if (ctx.historyRef.current.get(sid) !== state) return null;
    const prior = state.page;
    const current = ctx.storeRef.current[sid] ?? [];
    if (target && (!indexRevision || prior?.revision === indexRevision) &&
        current.some((row) => row.id === target)) return { id: target, block: null };
    if (action === "older" && !prior?.before) return null;
    const response = await getSessionHistoryPage(sid, {
      before: action === "latest" ? null : prior?.before,
      refresh: action === "latest" ? prior?.tail : null,
      revision: prior?.revision,
      coverage: prior?.coverage,
      target,
    });
    if (ctx.historyRef.current.get(sid) !== state) return null;
    validateHistory(response, action, prior);
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
