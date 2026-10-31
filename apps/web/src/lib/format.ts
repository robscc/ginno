// Token/number display formatting shared by the TopBar usage tooltip and the
// usage settings panels.

/** Compact human token counts: 950 → "950", 12_400 → "12.4k",
 * 1_250_000 → "1.25M", 3_000_000_000 → "3B". One decimal, trailing ".0"
 * trimmed; never scientific. Locale-independent (en-US digits) so logs and
 * screenshots stay greppable. */
export function fmtTokens(n: number | undefined | null): string {
  if (n == null || !Number.isFinite(n)) return "0";
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  const units: Array<[number, string]> = [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "k"],
  ];
  for (const [scale, suffix] of units) {
    if (abs >= scale) {
      const v = n / scale;
      const s = v >= 100 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, "");
      return `${s}${suffix}`;
    }
  }
  return String(n);
}
