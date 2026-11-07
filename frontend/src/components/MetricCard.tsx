import type { Metric } from "../api/types";
import { fmt, humanize } from "../lib/format";
import { Badge, metricTone } from "./ui/Badge";
import { SourceList } from "./ui/SourceList";

interface MetricCardProps {
  metric: Metric | null;
  /** Fallback title when the backend did not return this metric at all. */
  title: string;
  /** Optional override of what to display as the headline value. */
  display?: { value: unknown; unit?: string | null };
  footnote?: string;
}

/**
 * KPI card that always shows its evidence: sample size and source view are visible,
 * method, columns and missing/excluded counts are one click away.
 */
export function MetricCard({ metric, title, display, footnote }: MetricCardProps) {
  if (!metric) {
    return (
      <article className="panel flex flex-col gap-2 p-4" data-testid="metric-card">
        <h3 className="label-caps">{title}</h3>
        <p className="text-2xl font-semibold text-ink-500">—</p>
        <p className="text-xs text-ink-400">Not returned by the backend.</p>
      </article>
    );
  }
  const ok = metric.status === "ok";
  const value = display ? display.value : metric.value;
  const unit = display ? display.unit : metric.unit;
  return (
    <article className="panel flex min-w-0 flex-col gap-2 p-4" data-testid="metric-card" aria-label={metric.title}>
      <div className="flex items-start justify-between gap-2">
        <h3 className="label-caps">{metric.title}</h3>
        {!ok && <Badge tone={metricTone(metric.status)}>{metric.status}</Badge>}
      </div>
      {ok ? (
        <p className="num flex flex-wrap items-baseline gap-x-1.5 text-2xl font-semibold text-ink-100">
          <span data-testid="metric-value">{fmt(value)}</span>
          {unit && <span className="text-xs font-normal text-ink-400">{unit}</span>}
        </p>
      ) : (
        <p className="text-sm text-ink-300" data-testid="metric-reason">
          {metric.reason || "Not available."}
        </p>
      )}
      <div className="mt-auto space-y-1.5 border-t border-ink-800 pt-2">
        {metric.sample_size !== null && metric.sample_size !== undefined && (
          <p className="text-xs text-ink-400">
            Sample size <span className="num text-ink-200">n = {fmt(metric.sample_size, 0)}</span>
            {metric.sample_meaning && <span> ({metric.sample_meaning})</span>}
          </p>
        )}
        <SourceList sources={metric.sources} compact />
        {footnote && <p className="text-xs text-ink-500">{footnote}</p>}
        {(metric.limitations ?? []).map((l) => (
          <p key={l} className="text-xs text-signal-warn" data-testid="metric-limitation">{l}</p>
        ))}
        {(metric.method || Object.keys(metric.missing ?? {}).length > 0) && (
          <details className="group text-xs">
            <summary className="cursor-pointer select-none text-ink-400 hover:text-ink-200">How this is calculated</summary>
            <div className="mt-1.5 space-y-1.5 text-ink-400">
              {metric.method && <p className="leading-relaxed">{metric.method}</p>}
              {Object.entries(metric.missing ?? {}).map(([k, v]) => (
                <p key={k}>
                  {humanize(k)}: <span className="num text-ink-200">{fmt(v, 0)}</span>
                </p>
              ))}
              <SourceList sources={metric.sources} />
            </div>
          </details>
        )}
      </div>
    </article>
  );
}
