import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmt } from "../../lib/format";
import { EmptyBlock } from "../ui/States";
import { CHART } from "./theme";

export interface BarDatum {
  label: string;
  value: number | null;
  suppressed?: boolean;
}

/** Vertical bars for bucketed counts (duration buckets, problem categories). */
export function DistributionChart({ data, valueLabel, color = CHART.secondary, height = 220 }:
  { data: BarDatum[]; valueLabel: string; color?: string; height?: number }) {
  const rows = data.filter((d) => typeof d.value === "number");
  if (!rows.length) return <EmptyBlock message="No values to chart." />;
  const suppressed = data.filter((d) => d.suppressed).length;
  return (
    <div>
      <div style={{ height }} role="img" aria-label={`${valueLabel} distribution`}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
            <CartesianGrid stroke={CHART.grid} vertical={false} />
            <XAxis dataKey="label" tick={CHART.tick} stroke={CHART.grid} interval={0} />
            <YAxis tick={CHART.tick} stroke={CHART.grid} width={44} tickFormatter={(v) => fmt(v, 0)} />
            <Tooltip {...CHART.tooltip} formatter={(v: number) => [fmt(v, 0), valueLabel]} />
            <Bar dataKey="value" fill={color} radius={[3, 3, 0, 0]} maxBarSize={44} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      {suppressed > 0 && (
        <p className="mt-1 text-xs text-ink-500">{suppressed} bucket(s) suppressed for privacy (too few records).</p>
      )}
    </div>
  );
}
