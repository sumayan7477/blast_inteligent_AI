import { api } from "../api/client";
import type { AnalyticsFilters, AnalyticsSection, Metric } from "../api/types";
import { useAsync } from "./useAsync";

export function useAnalytics(section: AnalyticsSection, filters: AnalyticsFilters, enabled = true) {
  const key = enabled ? `${section}:${JSON.stringify(filters)}` : `${section}:disabled`;
  return useAsync(
    (signal) => (enabled ? api.analytics(section, filters, signal) : new Promise<never>(() => undefined)),
    key,
  );
}

export function findMetric<D = Record<string, unknown>>(metrics: Metric[] | undefined, key: string): Metric<D> | null {
  return (metrics?.find((m) => m.key === key) as Metric<D> | undefined) ?? null;
}
