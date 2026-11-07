import { useState, type FormEvent } from "react";
import { Filter, X } from "lucide-react";
import type { AnalyticsFilters } from "../api/types";
import { splitList } from "../lib/format";

interface Props {
  value: AnalyticsFilters;
  onChange: (f: AnalyticsFilters) => void;
  showIdentifiers?: boolean;
  showGrain?: boolean;
}

/**
 * Filters are sent to the backend in the request body (never in the URL) and
 * are kept in component memory only.
 */
export function FilterBar({ value, onChange, showIdentifiers = false, showGrain = false }: Props) {
  const [draft, setDraft] = useState({
    date_from: value.date_from ?? "",
    date_to: value.date_to ?? "",
    rigs: (value.rigs ?? []).join(", "),
    sites: (value.sites ?? []).join(", "),
    blasts: (value.blasts ?? []).join(", "),
    grain: value.grain ?? "month",
  });
  const invalidRange = Boolean(draft.date_from && draft.date_to && draft.date_from > draft.date_to);

  const apply = (e: FormEvent) => {
    e.preventDefault();
    if (invalidRange) return;
    onChange({
      date_from: draft.date_from || undefined,
      date_to: draft.date_to || undefined,
      rigs: showIdentifiers ? splitList(draft.rigs) : undefined,
      sites: showIdentifiers ? splitList(draft.sites) : undefined,
      blasts: showIdentifiers ? splitList(draft.blasts) : undefined,
      grain: showGrain ? (draft.grain as "month" | "week") : undefined,
    });
  };
  const reset = () => {
    setDraft({ date_from: "", date_to: "", rigs: "", sites: "", blasts: "", grain: "month" });
    onChange({});
  };

  return (
    <form onSubmit={apply} className="panel flex flex-wrap items-end gap-3 px-4 py-3" aria-label="Filters">
      <label className="flex flex-col gap-1">
        <span className="label-caps">From</span>
        <input type="date" className="input w-40" value={draft.date_from}
               onChange={(e) => setDraft({ ...draft, date_from: e.target.value })} aria-label="From date" />
      </label>
      <label className="flex flex-col gap-1">
        <span className="label-caps">To</span>
        <input type="date" className="input w-40" value={draft.date_to}
               onChange={(e) => setDraft({ ...draft, date_to: e.target.value })} aria-label="To date" />
      </label>
      {showIdentifiers && (
        <>
          {(["rigs", "sites", "blasts"] as const).map((k) => (
            <label key={k} className="flex min-w-[9rem] flex-1 flex-col gap-1 sm:max-w-[13rem]">
              <span className="label-caps">{k === "sites" ? "Field / site" : k.slice(0, -1)}</span>
              <input className="input" value={draft[k]} placeholder="comma separated"
                     onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} aria-label={`${k} filter`}
                     maxLength={400} autoComplete="off" spellCheck={false} />
            </label>
          ))}
        </>
      )}
      {showGrain && (
        <label className="flex flex-col gap-1">
          <span className="label-caps">Trend grain</span>
          <select className="input w-28" value={draft.grain}
                  onChange={(e) => setDraft({ ...draft, grain: e.target.value as "month" | "week" })} aria-label="Trend grain">
            <option value="month">Monthly</option>
            <option value="week">Weekly</option>
          </select>
        </label>
      )}
      <div className="flex items-center gap-2">
        <button type="submit" className="btn-primary" disabled={invalidRange}>
          <Filter className="h-3.5 w-3.5" aria-hidden /> Apply
        </button>
        <button type="button" className="btn" onClick={reset}>
          <X className="h-3.5 w-3.5" aria-hidden /> Reset
        </button>
      </div>
      {invalidRange && <p className="w-full text-xs text-signal-bad">“From” must be on or before “To”.</p>}
    </form>
  );
}
