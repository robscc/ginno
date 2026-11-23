type SummaryVersion = { requestId: string; revision: number };
type SummaryDelta = SummaryVersion & { baseRevision: number };

/** Track this socket's revisions and request a snapshot when a delta has a gap. */
export function trackReasoningSummaries(
  send: (frame: { type: "reasoning_summary_state"; requestId: string }) => void,
) {
  const revisions = new Map<string, number>();
  const missing = new Map<string, number>();

  function requestSnapshot(requestId: string) {
    send({ type: "reasoning_summary_state", requestId });
  }

  function noteSnapshot({ requestId, revision }: SummaryVersion) {
    revisions.set(requestId, Math.max(revision, revisions.get(requestId) ?? 0));
    const missingRevision = missing.get(requestId);
    if (missingRevision !== undefined && revision >= missingRevision) missing.delete(requestId);
  }

  return function accept(frame: {
    event: string;
    summaryDelta?: SummaryDelta;
    summary?: SummaryVersion;
    recovery?: boolean;
    reasoning_summaries?: SummaryVersion[];
  }): boolean {
    switch (frame.event) {
      case "message.end":
      case "turn.stopped":
      case "error":
        revisions.clear();
        missing.clear();
        return true;
      case "turn.state":
        frame.reasoning_summaries?.forEach(noteSnapshot);
        return true;
      case "reasoning.summary":
        if (frame.summary) {
          const { requestId } = frame.summary;
          // A probe can already have repaired the gap, or the turn may have ended.
          if (frame.recovery && !missing.has(requestId)) return false;
          noteSnapshot(frame.summary);
          if (frame.recovery && missing.has(requestId)) requestSnapshot(requestId);
        }
        if (frame.summaryDelta) {
          const { requestId, revision, baseRevision } = frame.summaryDelta;
          const known = revisions.get(requestId) ?? 0;
          if (revision <= known) return false;
          if (baseRevision !== known) {
            const pending = missing.get(requestId);
            if (pending === undefined) requestSnapshot(requestId);
            missing.set(requestId, Math.max(pending ?? 0, revision));
            return false;
          }
          revisions.set(requestId, revision);
        }
        return true;
      default:
        return true;
    }
  };
}
