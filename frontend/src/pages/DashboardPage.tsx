import { useState } from "react";
import { Activity, ArrowRight, BarChart3, Gauge, ShieldAlert, Sparkles, Timer } from "lucide-react";
import type {
  AnalyticsFilters, DistributionBucket, DurationDetails, Metric, RigDetails, TrendDetails,
} from "../api/types";
import { AnomalyReview } from "../components/AnomalyReview";
import { AsyncView, MetricBody } from "../components/Async";
import { DistributionChart } from "../components/charts/DistributionChart";
import { GroupBarChart } from "../components/charts/GroupBarChart";
import { CHART } from "../components/charts/theme";
import { TrendChart } from "../components/charts/TrendChart";
import { FilterBar } from "../components/FilterBar";
import { MetricCard } from "../components/MetricCard";
import { ReadinessPanel } from "../components/ReadinessPanel";
import { Panel } from "../components/ui/Panel";
import { ErrorBlock, LoadingBlock } from "../components/ui/States";
import { findMetric, useAnalytics } from "../hooks/useAnalytics";
import { fmt } from "../lib/format";
import { navigate } from "../lib/router";

const KPIS: { key: string; title: string }[] = [
  { key: "total_blasts", title: "Total blasts" },
  { key: "total_holes", title: "Total drilled holes" },
  { key: "completion_rate", title: "Completion rate" },
  { key: "problem_frequency", title: "Problem frequency" },
];

function MoreLink({ tab, label }: { tab: string; label: string }) {
  return (
    <button type="button" className="flex items-center gap-1 text-xs text-ore-300 hover:text-ore-400"
            onClick={() => navigate("analytics", tab)}>
      {label} <ArrowRight className="h-3 w-3" aria-hidden />
    </button>
  );
}

export function DashboardPage() {
  const [filters, setFilters] = useState<AnalyticsFilters>({});
  const overview = useAnalytics("overview", filters);
  const drilling = useAnalytics("drilling", filters);
  const rigs = useAnalytics("rigs", filters);
  const trends = useAnalytics("trends", filters);
  const anomalies = useAnalytics("anomalies", filters);
  const readiness = useAnalytics("prediction-readiness", filters);

  return (
    <div className="space-y-4">
      <FilterBar value={filters} onChange={setFilters} />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Key figures">
        {overview.status === "loading" &&
          KPIS.map((k) => (
            <div key={k.key} className="panel p-4"><p className="label-caps mb-3">{k.title}</p><LoadingBlock rows={1} label="Loading…" /></div>
          ))}
        {overview.status === "error" && (
          <div className="sm:col-span-2 xl:col-span-4"><ErrorBlock error={overview.error} onRetry={overview.reload} /></div>
        )}
        {overview.status === "success" &&
          KPIS.map((k) => <MetricCard key={k.key} title={k.title} metric={findMetric(overview.data.result.metrics, k.key)} />)}
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Panel title="Drilling performance" icon={Timer} className="xl:col-span-2" actions={<MoreLink tab="drilling" label="Details" />}>
          <AsyncView state={drilling} onRetry={drilling.reload}>
            {(d) => {
              const dur = findMetric<DurationDetails>(d.result.metrics, "drilling_duration");
              const depth = findMetric<{ mean_depth: number; median_depth: number }>(d.result.metrics, "drilled_depth");
              const rate = findMetric(d.result.metrics, "drilling_rate");
              return (
                <div className="grid gap-3 md:grid-cols-3">
                  <MetricCard title="Drilling duration" metric={dur as Metric | null}
                              footnote={dur?.details ? `Median ${fmt(dur.details.median_minutes)} min · P90 ${fmt(dur.details.p90_minutes)} min` : undefined} />
                  <MetricCard title="Drilled depth" metric={depth as Metric | null}
                              footnote={depth?.details ? `Mean ${fmt(depth.details.mean_depth)} · median ${fmt(depth.details.median_depth)}` : undefined} />
                  <MetricCard title="Drilling rate" metric={rate} />
                </div>
              );
            }}
          </AsyncView>
        </Panel>
        <Panel title="Completion time distribution" icon={Gauge}>
          <AsyncView state={drilling} onRetry={drilling.reload}>
            {(d) => (
              <MetricBody<{ distribution: DistributionBucket[] }> metric={findMetric(d.result.metrics, "completion_time_analysis")}
                                                                missingTitle="Completion time analysis">
                {(m) => (
                  <DistributionChart valueLabel="Holes"
                                     data={m.details.distribution.map((b) => ({ label: b.bucket, value: b.holes, suppressed: b.suppressed }))} />
                )}
              </MetricBody>
            )}
          </AsyncView>
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Rig performance comparison" subtitle="Median drilling duration per rig (minutes)" icon={BarChart3}
               actions={<MoreLink tab="rigs" label="All rig metrics" />}>
          <AsyncView state={rigs} onRetry={rigs.reload}>
            {(d) => (
              <MetricBody<RigDetails> metric={findMetric(d.result.metrics, "rig_performance")} missingTitle="Rig performance">
                {(m) => (
                  <>
                    <GroupBarChart rows={m.details.rigs} valueKey="median_duration_minutes" valueLabel="Median duration (min)" />
                    {m.details.rigs_suppressed_small_sample > 0 && (
                      <p className="mt-1 text-xs text-ink-500">{m.details.rigs_suppressed_small_sample} rig(s) with too few records suppressed.</p>
                    )}
                  </>
                )}
              </MetricBody>
            )}
          </AsyncView>
        </Panel>
        <Panel title="Historical trends" subtitle="Drilled holes and median duration per month" icon={Activity}
               actions={<MoreLink tab="trends" label="All trends" />}>
          <AsyncView state={trends} onRetry={trends.reload}>
            {(d) => (
              <MetricBody<TrendDetails> metric={findMetric(d.result.metrics, "trends")} missingTitle="Trends">
                {(m) => (
                  <TrendChart points={m.details.holes ?? []} series={[
                    { key: "holes", label: "Holes", color: CHART.primary },
                    { key: "median_duration_minutes", label: "Median duration (min)", color: CHART.secondary, axis: "right" },
                  ]} />
                )}
              </MetricBody>
            )}
          </AsyncView>
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Anomaly overview" icon={ShieldAlert} actions={<MoreLink tab="anomalies" label="Review" />}>
          <AsyncView state={anomalies} onRetry={anomalies.reload} loadingLabel="Running anomaly detectors…">
            {(d) => (d.result.anomalies ? <AnomalyReview result={d.result.anomalies} /> : null)}
          </AsyncView>
        </Panel>
        <Panel title="Prediction readiness" subtitle="Drilling-duration prediction" icon={Sparkles}
               actions={<MoreLink tab="prediction" label="Details" />}>
          <AsyncView state={readiness} onRetry={readiness.reload}>
            {(d) => (d.result.readiness ? <ReadinessPanel readiness={d.result.readiness} /> : null)}
          </AsyncView>
        </Panel>
      </div>
    </div>
  );
}
