import { useState, type FormEvent } from "react";
import {
  Activity, AlertOctagon, BarChart3, Bomb, GitCompare, MapPin, ShieldAlert, Sparkles, Timer,
} from "lucide-react";
import type {
  AnalyticsFilters, BlastComparisonRow, DistributionBucket, DistributionStats, DurationDetails, GroupRow, Metric,
  ProblemDetails, RigDetails, SiteDetails, TrendDetails,
} from "../api/types";
import { AnomalyReview } from "../components/AnomalyReview";
import { AsyncView, MetricBody } from "../components/Async";
import { DistributionChart } from "../components/charts/DistributionChart";
import { GroupBarChart } from "../components/charts/GroupBarChart";
import { CHART } from "../components/charts/theme";
import { TrendChart } from "../components/charts/TrendChart";
import { FilterBar } from "../components/FilterBar";
import { GroupTable } from "../components/GroupTable";
import { MetricCard } from "../components/MetricCard";
import { ReadinessPanel } from "../components/ReadinessPanel";
import { Badge } from "../components/ui/Badge";
import { Panel } from "../components/ui/Panel";
import { findMetric, useAnalytics } from "../hooks/useAnalytics";
import { fmt, humanize, splitList } from "../lib/format";
import { navigate } from "../lib/router";

export const TABS = [
  { id: "blasts", label: "Blasts", icon: Bomb },
  { id: "drilling", label: "Drilling", icon: Timer },
  { id: "rigs", label: "Rigs", icon: BarChart3 },
  { id: "sites", label: "Sites", icon: MapPin },
  { id: "trends", label: "Trends", icon: Activity },
  { id: "problems", label: "Problems", icon: AlertOctagon },
  { id: "anomalies", label: "Anomalies", icon: ShieldAlert },
  { id: "prediction", label: "Prediction", icon: Sparkles },
] as const;
type TabId = (typeof TABS)[number]["id"];

type F = { filters: AnalyticsFilters };

function BlastsTab({ filters }: F) {
  const blasts = useAnalytics("blasts", filters);
  const [idsText, setIdsText] = useState("");
  const [ids, setIds] = useState<string[]>([]);
  const compare = useAnalytics("compare-blasts", { ...filters, blast_ids: ids }, ids.length > 0);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setIds(splitList(idsText));
  };
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Panel title="Historical blast analysis" subtitle="Distribution of per-blast aggregates across all blasts" icon={Bomb}>
        <AsyncView state={blasts} onRetry={blasts.reload}>
          {(d) => (
            <MetricBody<{ per_blast_distribution: Record<string, DistributionStats> }>
              metric={findMetric(d.result.metrics, "blast_analysis")} missingTitle="Blast analysis">
              {(m) => (
                <div className="overflow-x-auto">
                  <p className="mb-2 text-sm text-ink-300"><span className="num font-semibold text-ink-100">{fmt(m.value)}</span> blasts analysed</p>
                  <table className="table-base min-w-[480px]">
                    <thead><tr><th scope="col">Per blast</th><th scope="col" className="text-right">Blasts</th>
                      <th scope="col" className="text-right">Mean</th><th scope="col" className="text-right">Median</th>
                      <th scope="col" className="text-right">P10</th><th scope="col" className="text-right">P90</th></tr></thead>
                    <tbody>
                      {Object.entries(m.details.per_blast_distribution).map(([k, s]) => (
                        <tr key={k}>
                          <td>{humanize(k)}</td>
                          <td className="num text-right">{fmt(s.blasts_with_value, 0)}</td>
                          {s.suppressed ? (
                            <td colSpan={4} className="text-right text-xs text-ink-500">suppressed (too few blasts)</td>
                          ) : (
                            <>
                              <td className="num text-right">{fmt(s.mean)}</td><td className="num text-right">{fmt(s.median)}</td>
                              <td className="num text-right">{fmt(s.p10)}</td><td className="num text-right">{fmt(s.p90)}</td>
                            </>
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </MetricBody>
          )}
        </AsyncView>
      </Panel>
      <Panel title="Blast comparison" subtitle="Percentile of each blast among all blasts under the same filters" icon={GitCompare}>
        <form onSubmit={submit} className="mb-3 flex flex-wrap gap-2">
          <label className="min-w-[12rem] flex-1">
            <span className="sr-only">Blast ids</span>
            <input className="input" value={idsText} onChange={(e) => setIdsText(e.target.value)}
                   placeholder="Blast ids, comma separated (max 10)" autoComplete="off" spellCheck={false} maxLength={700} />
          </label>
          <button type="submit" className="btn-primary" disabled={!splitList(idsText).length}>Compare</button>
        </form>
        {ids.length === 0 ? (
          <p className="text-xs text-ink-500">Enter blast identifiers to compare. Results show pseudonymised labels in request order.</p>
        ) : (
          <AsyncView state={compare} onRetry={compare.reload}>
            {(d) => (
              <MetricBody<{ blasts: BlastComparisonRow[]; not_found: number }>
                metric={findMetric(d.result.metrics, "blast_comparison")} missingTitle="Blast comparison">
                {(m) => (
                  <ul className="space-y-2">
                    {m.details.blasts.map((b) => (
                      <li key={b.requested_index} className="rounded-md border border-ink-800 bg-ink-850/50 p-3">
                        <p className="text-sm text-ink-100">
                          Request #{b.requested_index + 1}: {b.found ? <code className="font-mono">{b.blast}</code> : <span className="text-ink-400">not found</span>}
                        </p>
                        {b.found && (
                          <dl className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                            {Object.entries(b.percentile_vs_all_blasts ?? {}).map(([k, p]) => (
                              <div key={k}>
                                <dt className="text-ink-500">{humanize(k)}</dt>
                                <dd className="num text-ink-200">
                                  {p === null ? "—" : `P${fmt(p, 0)}`}
                                  {b.values?.[k] !== undefined && b.values?.[k] !== null && <span className="text-ink-400"> · {fmt(b.values[k])}</span>}
                                </dd>
                              </div>
                            ))}
                          </dl>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </MetricBody>
            )}
          </AsyncView>
        )}
      </Panel>
    </div>
  );
}

function DrillingTab({ filters }: F) {
  const drilling = useAnalytics("drilling", filters);
  return (
    <AsyncView state={drilling} onRetry={drilling.reload}>
      {(d) => {
        const dur = findMetric<DurationDetails>(d.result.metrics, "drilling_duration");
        const depth = findMetric<{ mean_depth: number; median_depth: number }>(d.result.metrics, "drilled_depth");
        return (
          <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-3">
              <MetricCard title="Drilling duration" metric={dur as Metric | null}
                          footnote={dur?.details ? `Median ${fmt(dur.details.median_minutes)} · P90 ${fmt(dur.details.p90_minutes)} · SD ${fmt(dur.details.stddev_minutes)} min` : undefined} />
              <MetricCard title="Drilled depth" metric={depth as Metric | null}
                          footnote={depth?.details ? `Mean ${fmt(depth.details.mean_depth)} · median ${fmt(depth.details.median_depth)}` : undefined} />
              <MetricCard title="Drilling rate" metric={findMetric(d.result.metrics, "drilling_rate")} />
            </div>
            <Panel title="Drilling completion time analysis" icon={Timer}>
              <MetricBody<{ distribution: DistributionBucket[] }> metric={findMetric(d.result.metrics, "completion_time_analysis")}
                                                                missingTitle="Completion time analysis">
                {(m) => (
                  <>
                    <p className="mb-2 text-sm text-ink-300"><span className="num font-semibold text-ink-100">{fmt(m.value)}%</span> of holes have a valid start → end interval</p>
                    <DistributionChart valueLabel="Holes" data={m.details.distribution.map((b) => ({ label: b.bucket, value: b.holes, suppressed: b.suppressed }))} />
                  </>
                )}
              </MetricBody>
            </Panel>
          </div>
        );
      }}
    </AsyncView>
  );
}

const RIG_MEASURES: { key: keyof GroupRow; label: string }[] = [
  { key: "median_duration_minutes", label: "Median duration (min)" },
  { key: "median_drilling_rate_per_hour", label: "Median rate per hour" },
  { key: "holes", label: "Holes" },
  { key: "completion_rate_pct", label: "Completion %" },
  { key: "utilization_pct", label: "Utilisation %" },
  { key: "problems_per_100_holes", label: "Problems per 100 holes" },
];

function GroupTab({ filters, kind }: F & { kind: "rigs" | "sites" }) {
  const state = useAnalytics(kind, filters);
  const [measure, setMeasure] = useState<keyof GroupRow>("median_duration_minutes");
  const key = kind === "rigs" ? "rig_performance" : "site_comparison";
  return (
    <Panel title={kind === "rigs" ? "Rig comparison" : "Field / site comparison"} icon={kind === "rigs" ? BarChart3 : MapPin}>
      <AsyncView state={state} onRetry={state.reload}>
        {(d) => (
          <MetricBody<RigDetails & SiteDetails> metric={findMetric(d.result.metrics, key)} missingTitle={key}>
            {(m) => {
              const rows = (kind === "rigs" ? m.details.rigs : m.details.sites) ?? [];
              const suppressed = kind === "rigs" ? m.details.rigs_suppressed_small_sample : m.details.sites_suppressed_small_sample;
              const measures = RIG_MEASURES.filter((x) => rows.some((r) => typeof r[x.key] === "number"));
              const active = measures.find((x) => x.key === measure) ?? measures[0];
              return (
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Measure">
                    {measures.map((x) => (
                      <button key={x.key} type="button" onClick={() => setMeasure(x.key)} aria-pressed={active?.key === x.key}
                              className={`rounded border px-2 py-1 text-xs ${active?.key === x.key ? "border-ore-500/60 bg-ore-500/10 text-ore-300" : "border-ink-700 text-ink-400 hover:text-ink-200"}`}>
                        {x.label}
                      </button>
                    ))}
                    <Badge>{m.details.labels} labels</Badge>
                  </div>
                  {active && <GroupBarChart rows={rows} valueKey={active.key} valueLabel={active.label} />}
                  <GroupTable rows={rows} groupLabel={kind === "rigs" ? "Rig" : "Site"} />
                  {suppressed > 0 && <p className="text-xs text-ink-500">{suppressed} group(s) suppressed for privacy (too few records).</p>}
                  {rows.some((r) => r.utilization_note) && (
                    <p className="text-xs text-ink-500">Utilisation is a proxy: merged drilling intervals ÷ time between the first start and last end.</p>
                  )}
                </div>
              );
            }}
          </MetricBody>
        )}
      </AsyncView>
    </Panel>
  );
}

function TrendsTab({ filters }: F) {
  const state = useAnalytics("trends", filters);
  return (
    <AsyncView state={state} onRetry={state.reload}>
      {(d) => (
        <MetricBody<TrendDetails> metric={findMetric(d.result.metrics, "trends")} missingTitle="Trends">
          {(m) => (
            <div className="grid gap-4 xl:grid-cols-2">
              {m.details.holes && (
                <Panel title="Drilled holes and duration" icon={Activity}>
                  <TrendChart points={m.details.holes} series={[
                    { key: "holes", label: "Holes", color: CHART.primary },
                    { key: "median_duration_minutes", label: "Median duration (min)", color: CHART.secondary, axis: "right" },
                  ]} />
                </Panel>
              )}
              {m.details.holes?.some((p) => typeof p.completion_rate_pct === "number") && (
                <Panel title="Completion rate" icon={Activity}>
                  <TrendChart points={m.details.holes} series={[{ key: "completion_rate_pct", label: "Completion %", color: CHART.tertiary }]} />
                </Panel>
              )}
              {m.details.blasts && (
                <Panel title="Blasts" icon={Bomb}>
                  <TrendChart points={m.details.blasts} series={[{ key: "blasts", label: "Blasts", color: CHART.primary }]} />
                </Panel>
              )}
              {m.details.problems && (
                <Panel title="Recorded problems" icon={AlertOctagon}>
                  <TrendChart points={m.details.problems} series={[{ key: "problems", label: "Problems", color: CHART.secondary }]} />
                </Panel>
              )}
            </div>
          )}
        </MetricBody>
      )}
    </AsyncView>
  );
}

function ProblemsTab({ filters }: F) {
  const state = useAnalytics("problems", filters);
  return (
    <AsyncView state={state} onRetry={state.reload}>
      {(d) => {
        const m = findMetric<ProblemDetails>(d.result.metrics, "problem_frequency");
        return (
          <div className="grid gap-4 xl:grid-cols-3">
            <MetricCard title="Problem frequency" metric={m as Metric | null} />
            <Panel title="Problems by category" subtitle="Category labels are pseudonymised unless revealed in backend settings" icon={AlertOctagon} className="xl:col-span-2">
              {m?.status === "ok" && m.details?.by_category ? (
                <>
                  <DistributionChart valueLabel="Records" color={CHART.primary}
                                     data={m.details.by_category.map((c) => ({ label: c.category, value: c.records }))} />
                  {(m.details.categories_suppressed ?? 0) > 0 && (
                    <p className="mt-1 text-xs text-ink-500">{m.details.categories_suppressed} small categories suppressed for privacy.</p>
                  )}
                </>
              ) : (
                <p className="text-xs text-ink-500">No category breakdown available (problem category not mapped, or metric unavailable).</p>
              )}
            </Panel>
          </div>
        );
      }}
    </AsyncView>
  );
}

function AnomaliesTab({ filters }: F) {
  const state = useAnalytics("anomalies", filters);
  return (
    <Panel title="Anomaly review" subtitle="Each flag lists its source file/line and the statistical reason" icon={ShieldAlert}>
      <AsyncView state={state} onRetry={state.reload} loadingLabel="Running anomaly detectors…">
        {(d) => (d.result.anomalies ? <AnomalyReview result={d.result.anomalies} detailed /> : null)}
      </AsyncView>
    </Panel>
  );
}

function PredictionTab({ filters }: F) {
  const state = useAnalytics("prediction-readiness", filters);
  return (
    <Panel title="Prediction readiness" subtitle="Drilling-duration target, features and leakage checks" icon={Sparkles}>
      <AsyncView state={state} onRetry={state.reload}>
        {(d) => (d.result.readiness ? <ReadinessPanel readiness={d.result.readiness} detailed /> : null)}
      </AsyncView>
    </Panel>
  );
}

export function AnalyticsPage({ tab }: { tab: string | null }) {
  const active: TabId = (TABS.find((t) => t.id === tab)?.id ?? "blasts") as TabId;
  const [filters, setFilters] = useState<AnalyticsFilters>({});
  return (
    <div className="space-y-4">
      <nav className="flex gap-1 overflow-x-auto border-b border-ink-800" aria-label="Analytics sections">
        {TABS.map((t) => (
          <button key={t.id} type="button" onClick={() => navigate("analytics", t.id)} aria-current={active === t.id ? "page" : undefined}
                  className={`flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm ${active === t.id ? "border-ore-500 text-ink-100" : "border-transparent text-ink-400 hover:text-ink-200"}`}>
            <t.icon className="h-3.5 w-3.5" aria-hidden /> {t.label}
          </button>
        ))}
      </nav>
      <FilterBar value={filters} onChange={setFilters} showIdentifiers showGrain={active === "trends"} />
      {active === "blasts" && <BlastsTab filters={filters} />}
      {active === "drilling" && <DrillingTab filters={filters} />}
      {active === "rigs" && <GroupTab filters={filters} kind="rigs" />}
      {active === "sites" && <GroupTab filters={filters} kind="sites" />}
      {active === "trends" && <TrendsTab filters={filters} />}
      {active === "problems" && <ProblemsTab filters={filters} />}
      {active === "anomalies" && <AnomaliesTab filters={filters} />}
      {active === "prediction" && <PredictionTab filters={filters} />}
    </div>
  );
}
