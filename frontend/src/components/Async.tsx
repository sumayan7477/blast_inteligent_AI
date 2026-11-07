import type { ReactNode } from "react";
import type { Metric } from "../api/types";
import type { AsyncState } from "../hooks/useAsync";
import { fmt, humanize } from "../lib/format";
import { SourceList } from "./ui/SourceList";
import { ErrorBlock, LoadingBlock, UnavailableBlock } from "./ui/States";

/** Renders loading / error / honest-unavailable states, then the children with data. */
export function AsyncView<T>({ state, onRetry, children, loadingLabel }: {
  state: AsyncState<T>;
  onRetry?: () => void;
  children: (data: T) => ReactNode;
  loadingLabel?: string;
}) {
  if (state.status === "loading") return <LoadingBlock label={loadingLabel} />;
  if (state.status === "error") return <ErrorBlock error={state.error} onRetry={onRetry} />;
  return <>{children(state.data)}</>;
}

/** Evidence footer for a metric: sample size, sources, method, missing counts - always visible. */
export function MetricEvidence({ metric }: { metric: Metric }) {
  return (
    <div className="mt-3 space-y-1.5 border-t border-ink-800 pt-2 text-xs text-ink-400" data-testid="metric-evidence">
      {metric.sample_size !== null && (
        <p>
          Sample size <span className="num text-ink-200">n = {fmt(metric.sample_size, 0)}</span>
        </p>
      )}
      {metric.method && <p className="leading-relaxed">{metric.method}</p>}
      {Object.entries(metric.missing ?? {}).length > 0 && (
        <p>
          {Object.entries(metric.missing).map(([k, v], i) => (
            <span key={k}>
              {i > 0 && " · "}
              {humanize(k)} <span className="num text-ink-200">{fmt(v, 0)}</span>
            </span>
          ))}
        </p>
      )}
      <SourceList sources={metric.sources} />
    </div>
  );
}

/** Shows a metric's unavailable/suppressed/error reason, or renders its content + evidence. */
export function MetricBody<D>({ metric, missingTitle, children }: {
  metric: Metric<D> | null;
  missingTitle: string;
  children: (m: Metric<D> & { details: D }) => ReactNode;
}) {
  if (!metric) return <UnavailableBlock title={missingTitle} reason="Not returned by the backend." />;
  if (metric.status !== "ok" || !metric.details) {
    if (metric.status === "ok") {
      return (
        <>
          <p className="num text-2xl font-semibold text-ink-100">
            {fmt(metric.value)} <span className="text-xs font-normal text-ink-400">{metric.unit}</span>
          </p>
          <MetricEvidence metric={metric as Metric} />
        </>
      );
    }
    return (
      <UnavailableBlock
        title={`${metric.title} — ${metric.status}`}
        reason={metric.reason}
      />
    );
  }
  return (
    <>
      {children(metric as Metric<D> & { details: D })}
      <MetricEvidence metric={metric as Metric} />
    </>
  );
}
