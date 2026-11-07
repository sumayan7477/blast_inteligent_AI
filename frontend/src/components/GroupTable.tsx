import type { GroupRow } from "../api/types";
import { fmt, fmtPct } from "../lib/format";

const COLUMNS: { key: keyof GroupRow; label: string; pct?: boolean }[] = [
  { key: "holes", label: "Holes" },
  { key: "median_duration_minutes", label: "Median duration (min)" },
  { key: "mean_depth", label: "Mean depth" },
  { key: "median_drilling_rate_per_hour", label: "Median rate /h" },
  { key: "completion_rate_pct", label: "Completion", pct: true },
  { key: "utilization_pct", label: "Utilisation", pct: true },
  { key: "problems_per_100_holes", label: "Problems /100 holes" },
  { key: "problem_flag_rate_pct", label: "Problem flags", pct: true },
  { key: "blasts", label: "Blasts" },
];

/** Table of rig/site rows. Columns with no values in any row are hidden (not mapped => not shown). */
export function GroupTable({ rows, groupLabel }: { rows: GroupRow[]; groupLabel: string }) {
  const cols = COLUMNS.filter((c) => rows.some((r) => r[c.key] !== undefined && r[c.key] !== null));
  return (
    <div className="overflow-x-auto">
      <table className="table-base min-w-[560px]">
        <thead>
          <tr>
            <th scope="col">{groupLabel}</th>
            {cols.map((c) => (
              <th key={c.key} scope="col" className="text-right">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.group}>
              <td className="font-mono text-xs text-ink-100">{r.group}</td>
              {cols.map((c) => (
                <td key={c.key} className="num text-right" title={c.key === "utilization_pct" ? r.utilization_note ?? undefined : undefined}>
                  {c.pct ? fmtPct(r[c.key] as number | null) : fmt(r[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
