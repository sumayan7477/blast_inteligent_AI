import { CheckCircle2, XCircle } from "lucide-react";
import type { Readiness } from "../api/types";
import { fmt } from "../lib/format";
import { Badge } from "./ui/Badge";

/** Prediction readiness as reported by predictions.assess_readiness - no model is trained from the UI. */
export function ReadinessPanel({ readiness, detailed = false }: { readiness: Readiness; detailed?: boolean }) {
  const q = readiness.target_quality;
  const usable = readiness.features.filter((f) => f.usable);
  return (
    <div className="space-y-3" data-testid="readiness">
      <div className="flex items-center gap-2">
        {readiness.ready ? (
          <CheckCircle2 className="h-5 w-5 text-signal-ok" aria-hidden />
        ) : (
          <XCircle className="h-5 w-5 text-ink-400" aria-hidden />
        )}
        <p className="text-sm font-semibold text-ink-100">
          {readiness.ready ? "Ready for a baseline model" : "Not ready for prediction"}
        </p>
      </div>
      <ul className="space-y-1 text-xs text-ink-300">
        {readiness.reasons.map((r) => (
          <li key={r}>• {r}</li>
        ))}
      </ul>
      {q && (
        <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
          {[
            ["Valid targets", q.valid_targets],
            ["Hole records", q.rows],
            ["Missing target", q.missing_target],
            ["Non-positive", q.invalid_targets_non_positive],
            ["Distinct values", q.distinct_values],
            ["Coeff. of variation", q.coefficient_of_variation],
          ].map(([label, value]) => (
            <div key={label as string} className="rounded border border-ink-800 bg-ink-850/60 px-2 py-1.5">
              <dt className="text-ink-500">{label}</dt>
              <dd className="num text-ink-100">{fmt(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {!detailed && readiness.features.length > 0 && (
        <p className="text-xs text-ink-400">
          {usable.length} of {readiness.features.length} candidate features usable
        </p>
      )}
      {detailed && (
        <>
          {readiness.target && (
            <p className="text-xs text-ink-400">
              Target <span className="text-ink-200">{readiness.target.name}</span>: {readiness.target.definition} ·{" "}
              <code className="font-mono">{readiness.target.view}</code>
            </p>
          )}
          {readiness.split_strategy && <p className="text-xs text-ink-400">Evaluation split: {readiness.split_strategy}</p>}
          {readiness.features.length > 0 && (
            <div className="overflow-x-auto">
              <table className="table-base min-w-[480px]">
                <thead>
                  <tr>
                    <th scope="col">Feature</th>
                    <th scope="col">Kind</th>
                    <th scope="col" className="text-right">Missing</th>
                    <th scope="col" className="text-right">Distinct</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {readiness.features.map((f) => (
                    <tr key={f.feature}>
                      <td className="font-mono text-xs">{f.feature}</td>
                      <td className="text-xs">{f.kind}</td>
                      <td className="num text-right">{f.missing_pct === null ? "—" : `${fmt(f.missing_pct, 1)}%`}</td>
                      <td className="num text-right">{fmt(f.distinct_values, 0)}</td>
                      <td>{f.usable ? <Badge tone="ok">usable</Badge> : <Badge title={f.note ?? undefined}>{f.note ?? "not usable"}</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {readiness.excluded.length > 0 && (
            <div>
              <p className="label-caps mb-1">Excluded to avoid leakage</p>
              <ul className="space-y-0.5 text-xs text-ink-400">
                {readiness.excluded.map((e) => (
                  <li key={e.role}>
                    <code className="font-mono text-ink-300">{e.column}</code> ({e.role}) — {e.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-xs text-ink-500">
            Model training runs only from the command line (<code className="font-mono">analytics.py run --train</code>)
            after readiness is confirmed.
          </p>
        </>
      )}
    </div>
  );
}
