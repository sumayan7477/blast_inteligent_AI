import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { GroupRow } from "../../api/types";
import { fmt } from "../../lib/format";
import { EmptyBlock } from "../ui/States";
import { CHART } from "./theme";

interface Props {
  rows: GroupRow[];
  valueKey: keyof GroupRow;
  valueLabel: string;
  color?: string;
}

/** Horizontal bar chart of one GroupRow measure (rigs or sites). Rows with no value are omitted. */
export function GroupBarChart({ rows, valueKey, valueLabel, color = CHART.primary }: Props) {
  const data = rows
    .filter((r) => typeof r[valueKey] === "number")
    .map((r) => ({ group: r.group, value: r[valueKey] as number }));
  if (!data.length) return <EmptyBlock message={`No ${valueLabel.toLowerCase()} values for these groups.`} />;
  const height = Math.max(160, data.length * 34 + 40);
  return (
    <div style={{ height }} role="img" aria-label={`${valueLabel} by group`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
          <CartesianGrid stroke={CHART.grid} horizontal={false} />
          <XAxis type="number" tick={CHART.tick} stroke={CHART.grid} tickFormatter={(v) => fmt(v, 1)} />
          <YAxis type="category" dataKey="group" width={96} tick={CHART.tick} stroke={CHART.grid} />
          <Tooltip {...CHART.tooltip} formatter={(v: number) => [fmt(v), valueLabel]} />
          <Bar dataKey="value" fill={color} radius={[0, 3, 3, 0]} maxBarSize={22} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
