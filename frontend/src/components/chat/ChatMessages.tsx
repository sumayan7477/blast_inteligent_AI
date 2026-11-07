import { AlertTriangle, Bot, Cpu, FileText, RotateCw, ShieldAlert, User } from "lucide-react";
import type { ChatMessage } from "../../hooks/useChat";
import { Badge } from "../ui/Badge";

export const OPERATION_LABELS: Record<string, string> = {
  inventory: "Dataset inventory",
  capabilities: "Metric availability",
  hole_summary: "Hole data summary",
  overview: "Overview",
  drilling_performance: "Drilling performance",
  rig_comparison: "Rig comparison",
  site_comparison: "Site comparison",
  blast_history: "Blast history",
  compare_blasts: "Blast comparison",
  trends: "Trends",
  anomalies: "Anomalies",
  problems: "Problems",
  prediction_readiness: "Prediction readiness",
  breakdown: "Breakdown",
  unsupported: "Not supported",
  rejected_injection: "Declined",
  rejected_raw: "Declined (raw records)",
};

export function operationLabel(op: string): string {
  return OPERATION_LABELS[op] ?? op.replace(/_/g, " ");
}

export function AnswerSourceBadge({ source, model }: { source: "llm" | "template"; model: string | null }) {
  return source === "llm" ? (
    <Badge tone="info" title="Answer phrased by the local Ollama model from the evidence only">
      <Cpu className="h-3 w-3" aria-hidden /> Answer: local LLM{model ? ` (${model})` : ""}
    </Badge>
  ) : (
    <Badge title="Deterministic answer built directly from the evidence (no LLM)">
      <FileText className="h-3 w-3" aria-hidden /> Answer: deterministic template
    </Badge>
  );
}

interface Props {
  messages: ChatMessage[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onRetry: (question: string) => void;
  pending: boolean;
}

export function ChatMessages({ messages, selectedId, onSelect, onRetry, pending }: Props) {
  return (
    <ol className="space-y-4" aria-label="Conversation" aria-live="polite">
      {messages.map((m) => {
        if (m.role === "user") {
          return (
            <li key={m.id} className="flex justify-end gap-2">
              <p className="max-w-[85%] whitespace-pre-wrap rounded-lg rounded-tr-sm bg-ink-700 px-3 py-2 text-sm text-ink-100">
                {m.text}
              </p>
              <User className="mt-1.5 h-4 w-4 shrink-0 text-ink-400" aria-label="You" />
            </li>
          );
        }
        if (m.role === "error") {
          return (
            <li key={m.id} className="flex gap-2" role="alert">
              <AlertTriangle className="mt-1.5 h-4 w-4 shrink-0 text-signal-bad" aria-hidden />
              <div className="flex-1 rounded-lg border border-signal-bad/40 bg-signal-bad/5 px-3 py-2">
                <p className="text-sm text-ink-100">{m.error.message}</p>
                <button type="button" className="btn mt-2" onClick={() => onRetry(m.question)} disabled={pending}>
                  <RotateCw className="h-3.5 w-3.5" aria-hidden /> Retry question
                </button>
              </div>
            </li>
          );
        }
        const r = m.response;
        const selected = m.id === selectedId;
        const flagged = r.statistical_anomalies.reduce((s, a) => s + (a.flagged_count ?? 0), 0);
        return (
          <li key={m.id} className="flex gap-2">
            <Bot className="mt-1.5 h-4 w-4 shrink-0 text-ore-400" aria-label="Assistant" />
            <div className={`min-w-0 flex-1 rounded-lg rounded-tl-sm border px-3 py-2.5 ${selected ? "border-ore-500/50 bg-ink-850" : "border-ink-700 bg-ink-900"}`}>
              <div className="mb-2 flex flex-wrap items-center gap-1.5" data-testid="answer-meta">
                <AnswerSourceBadge source={r.answer_source} model={r.llm.model} />{" "}
                <Badge tone={r.operation.startsWith("rejected") || r.operation === "unsupported" ? "neutral" : "accent"}>
                  Topic: {operationLabel(r.operation)}
                </Badge>{" "}
                {r.warnings.length > 0 && <Badge tone="warn">{r.warnings.length} warning(s)</Badge>}
              </div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-200" data-testid="answer">{r.answer}</p>
              {r.statistical_anomalies.length > 0 && (
                <p className="mt-2 flex items-start gap-1.5 text-xs text-signal-warn">
                  <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                  {flagged} statistical outlier(s) flagged for review — not confirmed operational problems.
                </p>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-ink-800 pt-2 text-xs text-ink-400">
                <span>{r.facts.length} facts</span>
                {r.unavailable.length > 0 && <span>{r.unavailable.length} unavailable</span>}
                {(r.limitations?.length ?? 0) > 0 && <span>{r.limitations!.length} limitation(s)</span>}
                <span className="num">{Math.round(r.elapsed_ms)} ms</span>
                <button type="button" className="ml-auto text-ore-300 hover:text-ore-400" onClick={() => onSelect(m.id)}
                        aria-pressed={selected}>
                  {selected ? "Showing evidence" : "Show evidence"}
                </button>
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
