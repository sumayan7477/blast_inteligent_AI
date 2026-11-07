import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { TrendPoint } from "../../api/types";
import { fmt } from "../../lib/format";
import { EmptyBlock } from "../ui/States";
import { CHART } from "./theme";

export interface TrendSeries {
  key: keyof TrendPoint;
  label: string;
  color: string;
  axis?: "left" | "right";
}

/** Line chart of backend trend points. Suppressed periods are shown as gaps, never filled in. */
export function TrendChart({ points, series, height = 240 }: { points: TrendPoint[]; series: TrendSeries[]; height?: number }) {
  const visible = series.filter((s) => points.some((p) => typeof p[s.key] === "number"));
  if (!points.length || !visible.length) return <EmptyBlock message="No trend periods returned for these filters." />;
  const hasRight = visible.some((s) => s.axis === "right");
  const data = points.map((p) => ({ ...p, period: p.period }));
  return (
    <div style={{ height }} role="img" aria-label={`Trend of ${visible.map((s) => s.label).join(", ")}`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: hasRight ? 8 : 16, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={CHART.grid} vertical={false} />
          <XAxis dataKey="period" tick={CHART.tick} stroke={CHART.grid} minTickGap={18} />
          <YAxis yAxisId="left" tick={CHART.tick} stroke={CHART.grid} width={48} tickFormatter={(v) => fmt(v, 0)} />
          {hasRight && (
            <YAxis yAxisId="right" orientation="right" tick={CHART.tick} stroke={CHART.grid} width={48}
                   tickFormatter={(v) => fmt(v, 0)} />
          )}
          <Tooltip {...CHART.tooltip} formatter={(v: number, name: string) => [fmt(v), name]} />
          <Legend wrapperStyle={{ fontSize: 12, color: CHART.axis }} />
          {visible.map((s) => (
            <Line key={String(s.key)} yAxisId={s.axis === "right" ? "right" : "left"} type="monotone"
                  dataKey={s.key as string} name={s.label} stroke={s.color} strokeWidth={2} dot={false}
                  connectNulls={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
