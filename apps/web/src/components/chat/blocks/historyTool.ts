import { getHistoryTool } from "@/lib/api/sessions";
import type { HistoryToolDetail } from "@/lib/api/sessions";

const inFlight = new Map<string, Promise<HistoryToolDetail>>();

/** Share concurrent reads across cards. Keep successful bodies in the mounted
 * card only, so reading through history does not accumulate a second transcript. */
export function loadHistoryTool(sessionId: string, key: string): Promise<HistoryToolDetail> {
  const scoped = JSON.stringify([sessionId, key]);
  const existing = inFlight.get(scoped);
  if (existing) return existing;
  const request = getHistoryTool(sessionId, key).finally(() => inFlight.delete(scoped));
  inFlight.set(scoped, request);
  return request;
}
