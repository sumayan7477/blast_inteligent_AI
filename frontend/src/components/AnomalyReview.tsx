import { AlertTriangle, FileSearch } from "lucide-react";
import type { AnomalyResult, Detector } from "../api/types";
import { fmt } from "../lib/format";
import { Badge } from "./ui/Badge";
import { SourceList } from "./ui/SourceList";
import { UnavailableBlock } from "./ui/States";

export const NOT_CONFIRMED = "Statistical outliers for human review — not confirmed operational problems.";

export function StatisticalNotice({ label }: { label?: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-signal-warn/30 bg-signal-warn/5 px-3 py-2 text-xs text-ink-300"
         role="note">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal-warn" aria-hidden />
      <span>{label || NOT_CONFIRMED}</span>
    </div>
  );
}

function DetectorRow({ d, detailed }: { d: Detector; detailed: boolean }) {
  if (d.status !== "ok") {
    return detailed ? <UnavailableBlock title={d.title} reason={d.reason} /> : null;
  }
  const flagged = d.flagged_count ?? 0;
  return (
    <li className="rounded-md border border-ink-800 bg-ink-850/50 p-3" data-testid="detector">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-ink-100">{d.title}</p>
        <div className="flex items-center gap-2">
          <Badge tone={flagged > 0 ? "warn" : "neutral"}>{fmt(flagged, 0)} flagged</Badge>
          {d.records_scored !== null && (
            <span className="num text-xs text-ink-400">of {fmt(d.records_scored, 0)} scored</span>
          )}
        </div>
      </div>
      {detailed && (
        <div className="mt-2 space-y-2">
          <p className="text-xs leading-relaxed text-ink-400">{d.method}</p>
          <SourceList sources={d.sources} />
          {d.flagged.length > 0 && (
            <div className="overflow-x-auto">
              <table className="table-base min-w-[520px]">
                <thead>
                  <tr>
                    <th scope="col">Reference</th>
                    <th scope="col">Why it was flagged</th>
                  </tr>
                </thead>
                <tbody>
                  {d.flagged.slice(0, 20).map((f, i) => (
                    <tr key={i}>
                      <td className="whitespace-nowrap font-mono text-xs text-ink-300">
                        {f.record ? `${f.record._source_file}:${f.record._source_line}` : f.group ?? "—"}
                      </td>
                      <td className="text-xs text-ink-300">{f.explanation}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {d.flagged.length > 20 && (
                <p className="mt-1 text-xs text-ink-500">Showing 20 of {d.flagged.length} listed flags.</p>
              )}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/** Anomaly overview (compact) or full review (detailed). Always labelled as statistical, never as confirmed. */
export function AnomalyReview({ result, detailed = false }: { result: AnomalyResult; detailed?: boolean }) {
  const ok = result.detectors.filter((d) => d.status === "ok");
  const unavailable = result.detectors.filter((d) => d.status !== "ok");
  return (
    <div className="space-y-3">
      <StatisticalNotice label={result.label} />
      {ok.length === 0 && !detailed && (
        <UnavailableBlock title="No anomaly detector could run" reason={unavailable[0]?.reason} />
      )}
      <ul className="space-y-2">
        {(detailed ? result.detectors : ok).map((d) => (
          <DetectorRow key={d.detector} d={d} detailed={detailed} />
        ))}
      </ul>
      {!detailed && unavailable.length > 0 && ok.length > 0 && (
        <p className="flex items-center gap-1.5 text-xs text-ink-500">
          <FileSearch className="h-3.5 w-3.5" aria-hidden />
          {unavailable.length} detector(s) unavailable with the current mapping.
        </p>
      )}
    </div>
  );
}
