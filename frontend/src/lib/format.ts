const nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const nf1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

export const DASH = "—";

export function fmt(value: unknown, digits: 0 | 1 | 2 = 2): string {
  if (value === null || value === undefined || value === "") return DASH;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return DASH;
    if (digits === 0) return Math.round(value).toLocaleString("en-US");
    return (digits === 1 ? nf1 : nf).format(value);
  }
  return String(value);
}

export function fmtPct(value: number | null | undefined): string {
  return value === null || value === undefined ? DASH : `${nf1.format(value)}%`;
}

export function humanize(key: string): string {
  return key
    .replace(/^m_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function splitList(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 10);
}
