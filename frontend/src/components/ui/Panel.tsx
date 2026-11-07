import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

interface PanelProps {
  title: string;
  subtitle?: string;
  icon?: LucideIcon;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

export function Panel({ title, subtitle, icon: Icon, actions, children, className = "", bodyClassName = "" }: PanelProps) {
  return (
    <section className={`panel flex min-w-0 flex-col ${className}`} aria-label={title}>
      <header className="flex items-start justify-between gap-3 border-b border-ink-800 px-4 py-3">
        <div className="flex min-w-0 items-start gap-2.5">
          {Icon && <Icon className="mt-0.5 h-4 w-4 shrink-0 text-ore-400" aria-hidden />}
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-ink-100">{title}</h2>
            {subtitle && <p className="mt-0.5 text-xs text-ink-400">{subtitle}</p>}
          </div>
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </header>
      <div className={`min-w-0 flex-1 p-4 ${bodyClassName}`}>{children}</div>
    </section>
  );
}
