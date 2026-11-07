import { AlertTriangle, CircleSlash, Inbox, Loader2, RotateCw } from "lucide-react";
import type { ApiError } from "../../api/client";

export function LoadingBlock({ label = "Loading from local database…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2">
      <div className="flex items-center gap-2 text-xs text-ink-400">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
        {label}
      </div>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-3 rounded bg-ink-800" style={{ width: `${90 - i * 18}%` }} />
      ))}
    </div>
  );
}

export function ErrorBlock({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  if (error.isUnavailable) return <UnavailableBlock title="Unavailable" reason={error.message} />;
  return (
    <div role="alert" className="flex items-start gap-3 rounded-md border border-signal-bad/40 bg-signal-bad/5 p-3">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-signal-bad" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink-100">{error.message}</p>
        {error.status && <p className="mt-0.5 text-xs text-ink-400">HTTP {error.status}</p>}
      </div>
      {onRetry && (
        <button type="button" className="btn shrink-0" onClick={onRetry}>
          <RotateCw className="h-3.5 w-3.5" aria-hidden /> Retry
        </button>
      )}
    </div>
  );
}

export function UnavailableBlock({ title, reason }: { title: string; reason: string | null | undefined }) {
  return (
    <div
      className="flex items-start gap-3 rounded-md border border-dashed border-ink-600 bg-ink-850/60 p-3"
      data-testid="unavailable"
    >
      <CircleSlash className="mt-0.5 h-4 w-4 shrink-0 text-ink-400" aria-hidden />
      <div className="min-w-0">
        <p className="text-sm font-medium text-ink-200">{title}</p>
        <p className="mt-0.5 text-xs text-ink-400">{reason || "Not available from the current data mapping."}</p>
      </div>
    </div>
  );
}

export function EmptyBlock({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm text-ink-400">
      <Inbox className="h-4 w-4" aria-hidden />
      {message}
    </div>
  );
}
