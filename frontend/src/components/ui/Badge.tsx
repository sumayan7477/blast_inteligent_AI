import type { ReactNode } from "react";

export type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "accent";

const TONES: Record<Tone, string> = {
  neutral: "border-ink-600 bg-ink-800 text-ink-300",
  ok: "border-signal-ok/40 bg-signal-ok/10 text-signal-ok",
  warn: "border-signal-warn/40 bg-signal-warn/10 text-signal-warn",
  bad: "border-signal-bad/40 bg-signal-bad/10 text-signal-bad",
  info: "border-signal-info/40 bg-signal-info/10 text-signal-info",
  accent: "border-ore-500/40 bg-ore-500/10 text-ore-300",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function StatusDot({ tone }: { tone: Tone }) {
  const color = {
    neutral: "bg-ink-500",
    ok: "bg-signal-ok",
    warn: "bg-signal-warn",
    bad: "bg-signal-bad",
    info: "bg-signal-info",
    accent: "bg-ore-500",
  }[tone];
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`} aria-hidden />;
}

export function metricTone(status: string): Tone {
  return status === "ok" ? "ok" : status === "suppressed" ? "warn" : status === "error" ? "bad" : "neutral";
}
