import { Database } from "lucide-react";
import type { SourceRef } from "../../api/types";

/** Shows where a number came from: typed view, dataset and columns. */
export function SourceList({ sources, compact = false }: { sources: SourceRef[]; compact?: boolean }) {
  if (!sources?.length) return null;
  return (
    <ul className="space-y-1" aria-label="Data sources">
      {sources.map((s, i) => (
        <li key={`${s.view}-${i}`} className="flex min-w-0 items-start gap-1.5 text-xs text-ink-400">
          <Database className="mt-0.5 h-3 w-3 shrink-0 text-ink-500" aria-hidden />
          <span className="min-w-0 break-words">
            <code className="font-mono text-ink-300">{s.view ?? "unknown view"}</code>
            {s.dataset && !compact && <span className="text-ink-500"> · {s.dataset}</span>}
            {s.columns?.length > 0 && (
              <span className="text-ink-500">
                {" "}
                · {compact && s.columns.length > 3 ? `${s.columns.slice(0, 3).join(", ")} +${s.columns.length - 3}` : s.columns.join(", ")}
              </span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}
