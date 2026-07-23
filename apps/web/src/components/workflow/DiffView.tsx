"use client";

/** Minimal unified-diff viewer: colorizes lines by their leading marker.
 *  Pure presentational — the server produces the diff text (difflib unified). */
export function DiffView({ diff }: { diff: string }) {
  const lines = (diff || "").split("\n");
  if (!diff || !diff.trim()) {
    return <div className="py-2 text-center text-[11px] text-faint">无差异</div>;
  }
  return (
    <pre className="max-h-72 overflow-auto rounded-lg border border-line bg-base/60 p-2 font-mono text-[11px] leading-relaxed">
      {lines.map((ln, i) => {
        let cls = "text-muted";
        if (ln.startsWith("+++") || ln.startsWith("---")) cls = "text-faint";
        else if (ln.startsWith("@@")) cls = "text-violet";
        else if (ln.startsWith("+")) cls = "text-green";
        else if (ln.startsWith("-")) cls = "text-red";
        return (
          <div key={i} className={cls}>
            {ln || " "}
          </div>
        );
      })}
    </pre>
  );
}
