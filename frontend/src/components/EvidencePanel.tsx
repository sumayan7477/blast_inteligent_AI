import { AlertTriangle, CircleSlash, FileCheck2, Info, Lightbulb, ShieldAlert } from "lucide-react";
import type { ChatResponse, CountKind } from "../api/types";
import { fmt } from "../lib/format";
import { StatisticalNotice } from "./AnomalyReview";
import { Badge } from "./ui/Badge";
import { SourceList } from "./ui/SourceList";
import { EmptyBlock } from "./ui/States";

export const COUNT_KIND_LABEL: Record<CountKind, string> = {
  total_rows: "Total records (all rows)",
  filtered_rows: "Records matching the filters",
  non_null: "Records with a value",
  distinct: "Distinct entities",
  sample: "Statistical sample",
  coverage: "Time coverage",
  metric: "Metric",
};

function SectionTitle({ icon: Icon, children, count }: { icon: typeof FileCheck2; children: string; count: number }) {
  return (
    <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ink-300">
      <Icon className="h-3.5 w-3.5 text-ore-400" aria-hidden />
      {children}
      <span className="num rounded bg-ink-800 px-1.5 text-[10px] text-ink-400">{count}</span>
    </h3>
  );
}

/**
 * Everything the backend returned as evidence, shown independently of the AI wording:
 * facts (metric, value, n, method, view, columns), statistical anomalies, unavailable
 * metrics with reasons, suggestions and warnings.
 */
export function EvidencePanel({ response }: { response: ChatResponse | null }) {
  if (!response) {
    return <EmptyBlock message="Ask a question to see the evidence behind the answer." />;
  }
  const { facts, statistical_anomalies: anomalies, unavailable, suggestions, warnings } = response;
  const limitations = response.limitations ?? [];
  return (
    <div className="space-y-5" data-testid="evidence-panel">
      <div className="flex flex-wrap gap-1.5">
        <Badge tone="accent">operation: {response.operation}</Badge>
        <Badge>intent: {response.intent_source}</Badge>
        <Badge tone={response.privacy.labels === "real" ? "warn" : "neutral"}>labels: {response.privacy.labels}</Badge>
        {response.privacy.min_group_size !== null && <Badge>groups &lt; {response.privacy.min_group_size} suppressed</Badge>}
      </div>

      {warnings.length > 0 && (
        <section aria-label="Warnings">
          <SectionTitle icon={AlertTriangle} count={warnings.length}>Warnings</SectionTitle>
          <ul className="space-y-1">
            {warnings.map((w, i) => (
              <li key={i} className="rounded border border-signal-warn/30 bg-signal-warn/5 px-2.5 py-1.5 text-xs text-ink-200">{w}</li>
            ))}
          </ul>
        </section>
      )}

      <section aria-label="Facts">
        <SectionTitle icon={FileCheck2} count={facts.length}>Facts from the database</SectionTitle>
        {facts.length === 0 ? (
          <p className="text-xs text-ink-500">No facts were returned for this question.</p>
        ) : (
          <ol className="space-y-2">
            {facts.map((f, i) => (
              <li key={i} className="rounded-md border border-ink-800 bg-ink-850/50 p-2.5" data-testid="fact">
                <p className="text-sm text-ink-100">{f.statement}</p>
                <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
                  {f.metric && (<><dt className="text-ink-500">Metric</dt><dd className="font-mono text-ink-300">{f.metric}</dd></>)}
                  {f.value !== null && f.value !== undefined && (
                    <><dt className="text-ink-500">Value</dt><dd className="num text-ink-200">{fmt(f.value)}{f.unit ? ` ${f.unit}` : ""}</dd></>
                  )}
                  {f.count_kind && f.count_kind !== "metric" && (
                    <><dt className="text-ink-500">Count type</dt><dd className="text-ink-300">{COUNT_KIND_LABEL[f.count_kind]}</dd></>
                  )}
                  <dt className="text-ink-500">Sample size</dt>
                  <dd className="num text-ink-200">
                    {f.sample_size !== null ? `n = ${fmt(f.sample_size, 0)}` : "—"}
                    {f.sample_size !== null && f.sample_meaning && <span className="text-ink-400"> ({f.sample_meaning})</span>}
                  </dd>
                  {f.method && (<><dt className="text-ink-500">Method</dt><dd className="text-ink-400">{f.method}</dd></>)}
                </dl>
                <div className="mt-1.5"><SourceList sources={f.sources} /></div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {anomalies.length > 0 && (
        <section aria-label="Statistical anomalies">
          <SectionTitle icon={ShieldAlert} count={anomalies.length}>Statistical anomalies</SectionTitle>
          <StatisticalNotice label={anomalies[0].label} />
          <ul className="mt-2 space-y-2">
            {anomalies.map((a) => (
              <li key={a.detector} className="rounded-md border border-signal-warn/25 bg-ink-850/50 p-2.5" data-testid="anomaly">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm text-ink-100">{a.title}</p>
                  <Badge tone={(a.flagged_count ?? 0) > 0 ? "warn" : "neutral"}>
                    {fmt(a.flagged_count, 0)} flagged{a.records_scored !== null ? ` / ${fmt(a.records_scored, 0)}` : ""}
                  </Badge>
                </div>
                <p className="mt-1 text-xs text-ink-400">{a.method}</p>
                {a.examples.length > 0 && (
                  <ul className="mt-1.5 space-y-1">
                    {a.examples.map((ex, i) => (
                      <li key={i} className="text-xs text-ink-300">
                        <code className="font-mono text-ink-400">{ex.reference}</code> — {ex.explanation}
                      </li>
                    ))}
                  </ul>
                )}
                <div className="mt-1.5"><SourceList sources={a.sources} /></div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {limitations.length > 0 && (
        <section aria-label="Limitations">
          <SectionTitle icon={Info} count={limitations.length}>Limitations</SectionTitle>
          <ul className="space-y-1.5">
            {limitations.map((l, i) => (
              <li key={i} className="rounded border border-ink-700 px-2.5 py-1.5 text-xs text-ink-300" data-testid="limitation">
                {l.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {unavailable.length > 0 && (
        <section aria-label="Unavailable">
          <SectionTitle icon={CircleSlash} count={unavailable.length}>Unavailable</SectionTitle>
          <ul className="space-y-1.5">
            {unavailable.map((u, i) => (
              <li key={i} className="rounded border border-dashed border-ink-600 px-2.5 py-1.5 text-xs" data-testid="unavailable-item">
                <span className="font-medium text-ink-200">{u.title}</span>
                <span className="text-ink-400"> — {u.reason}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {suggestions.length > 0 && (
        <section aria-label="Suggestions">
          <SectionTitle icon={Lightbulb} count={suggestions.length}>Suggestions (data / review only)</SectionTitle>
          <ul className="space-y-1.5">
            {suggestions.map((s, i) => (
              <li key={i} className="text-xs text-ink-300">
                <Badge>{s.category === "data_mapping" ? "data mapping" : s.category}</Badge> {s.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {response.evidence.sources.length > 0 && (
        <section aria-label="All sources">
          <h3 className="label-caps mb-1">All sources used</h3>
          <SourceList sources={response.evidence.sources} />
        </section>
      )}
    </div>
  );
}
