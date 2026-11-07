import type { ReactNode } from "react";
import { AlertTriangle, BarChart3, Bot, Cpu, Database, LayoutDashboard, Mountain, ShieldCheck } from "lucide-react";
import { API_BASE_URL, isLocalUrl } from "../../api/client";
import type { SystemStatus } from "../../hooks/useSystemStatus";
import { navigate, type Page } from "../../lib/router";
import { StatusDot, type Tone } from "../ui/Badge";

const NAV: { page: Page; label: string; icon: typeof LayoutDashboard }[] = [
  { page: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { page: "analytics", label: "Analytics", icon: BarChart3 },
  { page: "assistant", label: "AI Assistant", icon: Bot },
];

function statusRows(s: SystemStatus): { label: string; tone: Tone; text: string; icon: typeof Database }[] {
  const h = s.health;
  const ai = s.ai;
  return [
    {
      label: "Backend",
      icon: ShieldCheck,
      tone: s.loading ? "neutral" : h ? (h.status === "ok" ? "ok" : "warn") : "bad",
      text: s.loading ? "checking…" : h ? `${h.status} · v${h.version}` : "unreachable",
    },
    {
      label: "Database",
      icon: Database,
      tone: h ? (h.database.available && h.analytics_mapping.available ? "ok" : "warn") : "neutral",
      text: h
        ? h.database.available
          ? `${h.approved_views} views · ${h.analytics_mapping.confirmed_entities.length} entities`
          : "not available"
        : "—",
    },
    {
      label: "Local LLM",
      icon: Cpu,
      tone: ai ? (ai.reachable && ai.model_available ? "ok" : "warn") : s.loading ? "neutral" : "bad",
      text: ai
        ? ai.reachable
          ? ai.model_available
            ? ai.model ?? "ready"
            : "model missing"
          : "offline"
        : s.loading
          ? "checking…"
          : "unknown",
    },
  ];
}

export function AppShell({ page, status, children }: { page: Page; status: SystemStatus; children: ReactNode }) {
  const rows = statusRows(status);
  const local = isLocalUrl(API_BASE_URL);
  const title = NAV.find((n) => n.page === page)?.label ?? "";
  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <aside className="border-b border-ink-800 bg-ink-900 lg:sticky lg:top-0 lg:flex lg:h-screen lg:w-60 lg:shrink-0 lg:flex-col lg:border-b-0 lg:border-r">
        <div className="flex items-center gap-2.5 px-4 py-3 lg:py-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-ore-500/15 ring-1 ring-ore-500/40">
            <Mountain className="h-4 w-4 text-ore-400" aria-hidden />
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold text-ink-100">Blast Intelligence</p>
            <p className="text-[11px] text-ink-400">Drilling &amp; blasting analytics</p>
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:px-3" aria-label="Main">
          {NAV.map((n) => (
            <button key={n.page} type="button" onClick={() => navigate(n.page)} aria-current={page === n.page ? "page" : undefined}
                    className={`flex shrink-0 items-center gap-2.5 rounded-md px-3 py-2 text-sm ${page === n.page ? "bg-ink-800 text-ink-100" : "text-ink-400 hover:bg-ink-850 hover:text-ink-200"}`}>
              <n.icon className={`h-4 w-4 ${page === n.page ? "text-ore-400" : ""}`} aria-hidden />
              {n.label}
            </button>
          ))}
        </nav>
        <div className="mt-auto hidden border-t border-ink-800 px-4 py-4 lg:block" aria-label="System status">
          <p className="label-caps mb-2">System</p>
          <ul className="space-y-2">
            {rows.map((r) => (
              <li key={r.label} className="flex items-center gap-2 text-xs">
                <StatusDot tone={r.tone} />
                <span className="text-ink-400">{r.label}</span>
                <span className="ml-auto truncate text-right text-ink-200" title={r.text}>{r.text}</span>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[11px] leading-snug text-ink-500">Local-only · no cloud services · labels pseudonymised by the backend.</p>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-800 px-4 py-3 sm:px-6">
          <h1 className="text-lg font-semibold text-ink-100">{title}</h1>
          <ul className="flex flex-wrap items-center gap-3 lg:hidden" aria-label="System status">
            {rows.map((r) => (
              <li key={r.label} className="flex items-center gap-1.5 text-xs text-ink-300">
                <StatusDot tone={r.tone} /> {r.label}: {r.text}
              </li>
            ))}
          </ul>
        </header>
        {!local && (
          <div role="alert" className="mx-4 mt-4 flex items-start gap-2 rounded-md border border-signal-bad/40 bg-signal-bad/5 px-3 py-2 text-sm text-ink-100 sm:mx-6">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-signal-bad" aria-hidden />
            The API base URL ({API_BASE_URL}) is not a local address. This application is intended to talk to a local backend only.
          </div>
        )}
        {status.healthError && !status.loading && (
          <div role="alert" className="mx-4 mt-4 rounded-md border border-signal-bad/40 bg-signal-bad/5 px-3 py-2 text-sm text-ink-100 sm:mx-6">
            {status.healthError.message} Start it with <code className="font-mono text-ink-200">python backend\main.py</code>.
          </div>
        )}
        <div className="p-4 sm:p-6">{children}</div>
      </main>
    </div>
  );
}
