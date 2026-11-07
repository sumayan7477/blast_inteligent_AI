/**
 * SYNTHETIC fixtures shaped exactly like backend responses. All numbers and labels are
 * invented for tests and are never used by the application at runtime.
 */
import { vi } from "vitest";
import type { AiStatusResponse, AnalyticsResponse, ChatResponse, HealthResponse, Metric } from "../api/types";

const src = (view: string, columns: string[]) => [{ view, dataset: `${view.replace("tv_", "")}.jsonl`, columns }];

export const metric = (over: Partial<Metric>): Metric => ({
  key: "total_holes", title: "Total drilled holes", status: "ok", value: 1209, unit: "holes",
  method: "COUNT(DISTINCT hole_id) over the holes view", sources: src("tv_holes", ["holeId"]),
  sample_size: 1209, missing: { rows_without_hole_id: 0 }, reason: null, details: null, ...over,
});

export const overview: AnalyticsResponse = {
  section: "overview",
  filters: { date_from: null, date_to: null, rig_values: 0, site_values: 0, blast_values: 0, grain: "month", blast_ids_requested: 0 },
  privacy: { labels: "pseudonymised", min_group_size: 5, raw_records_returned: false },
  result: {
    metrics: [
      metric({ key: "total_blasts", title: "Total blasts", value: 40, unit: "blasts", sample_size: 40,
               sources: src("tv_blasts", ["blastId"]) }),
      metric({}),
      metric({ key: "completion_rate", title: "Drilling completion rate", value: 89.53, unit: "%", sample_size: 1185,
               method: "share of holes whose status is one of the 1 configured completed values",
               details: { completed_holes: 1061 } }),
      metric({ key: "problem_frequency", title: "Frequency of recorded drilling problems", status: "unavailable",
               value: null, unit: null, sample_size: null, sources: [], missing: {},
               reason: "no confirmed problems entity and no holes.problem_flag mapped" }),
    ],
  },
};

export const rigs: AnalyticsResponse = {
  ...overview, section: "rigs",
  result: {
    metrics: [metric({
      key: "rig_performance", title: "Rig performance", value: 2, unit: "rigs reported", sample_size: 600,
      details: {
        rigs: [
          { group: "RIG-aaa111", holes: 320, median_duration_minutes: 28.3, mean_depth: 14.1, median_drilling_rate_per_hour: 30.1, completion_rate_pct: 87.3 },
          { group: "RIG-bbb222", holes: 280, median_duration_minutes: 56.3, mean_depth: 13.9, median_drilling_rate_per_hour: 15.0, completion_rate_pct: 89.3 },
        ],
        rigs_suppressed_small_sample: 1, labels: "pseudonymised",
      },
    })],
  },
};

export const anomalies: AnalyticsResponse = {
  ...overview, section: "anomalies",
  result: {
    anomalies: {
      label: "Statistical outlier for human review - NOT a confirmed operational problem.",
      robust_z_threshold: 3.5,
      detectors: [
        { detector: "hole_duration", title: "Unusual drilling duration", status: "ok", reason: null,
          method: "robust z = (x - median) / (1.4826 * MAD) within each rig", sources: src("tv_holes", ["rigId"]),
          records_scored: 1205, flagged_count: 5,
          flagged: [{ record: { _record_key: "k1", _source_file: "holes.jsonl", _source_line: 51 }, group: "RIG-aaa111",
                      robust_z: 9.1, percentile: 99.9, explanation: "drilling duration is 9.1 robust SDs above the median of rig RIG-aaa111 (n=320); percentile 99.9",
                      label: "Statistical outlier for human review - NOT a confirmed operational problem." }] },
        { detector: "isolation_forest", title: "Multivariate outliers (Isolation Forest)", status: "unavailable",
          reason: "needs >= 500 hole records (have 120)", method: "", sources: [], records_scored: null, flagged_count: null, flagged: [] },
      ],
    },
  },
};

export const readiness: AnalyticsResponse = {
  ...overview, section: "prediction-readiness",
  result: {
    readiness: {
      ready: false, target: { name: "drilling_duration_minutes", definition: "end_time minus start_time, in minutes", columns: ["drillStart", "drillEnd"], view: "tv_holes" },
      reasons: ["only 120 valid duration targets (need >= 300)"],
      features: [{ feature: "depth", kind: "numeric", missing_pct: 0, distinct_values: 118, usable: true, note: null },
                 { feature: "site", kind: "categorical", missing_pct: 72.5, distinct_values: 3, usable: false, note: "more than 50% missing" }],
      excluded: [{ role: "end_time", column: "drillEnd", reason: "known only after drilling finishes" }],
      target_quality: { rows: 130, rows_with_target: 125, valid_targets: 120, invalid_targets_non_positive: 5, missing_target: 5, distinct_values: 119, coefficient_of_variation: 0.42 },
      split_strategy: "time-based: earliest 80% by start_time for training, latest 20% for testing",
    },
  },
};

export const health: HealthResponse = {
  status: "ok", version: "0.8.0", database: { available: true, read_only: true, error: null },
  analytics_mapping: { available: true, confirmed_entities: ["blasts", "holes"], error: null }, approved_views: 3, active_sessions: 0,
};

export const aiOffline: AiStatusResponse = {
  provider: "ollama", base_url: "http://127.0.0.1:11434", local_only: true, cloud_fallback: false, model: "llama3.1:8b",
  reachable: false, model_available: false, available_models: [], error: "connection_error", chat_uses_llm: true,
};

export const chatResponse = (over: Partial<ChatResponse> = {}): ChatResponse => ({
  session_id: "sess_synthetic_0001", operation: "overview", intent_source: "rules",
  filters: { date_from: null, date_to: null, rig_values: 0, site_values: 0, blast_values: 0, grain: "month", blast_ids_requested: 0, group_by: null },
  answer: "Result for: totals.\nFacts (from the database):\n- Total drilled holes: 1,209 holes (n=1,209)",
  answer_source: "template",
  facts: [{ type: "fact", statement: "Total drilled holes: 1,209 holes", metric: "total_holes", value: 1209, unit: "holes",
            sample_size: 1209, sources: src("tv_holes", ["holeId"]), method: "COUNT(DISTINCT hole_id) over the holes view" }],
  statistical_anomalies: [],
  unavailable: [{ type: "unavailable", metric: "problem_frequency", title: "Frequency of recorded drilling problems", status: "unavailable",
                  reason: "no confirmed problems entity and no holes.problem_flag mapped" }],
  suggestions: [{ type: "suggestion", category: "data_mapping", text: "Map and confirm problems in analytics_mapping.json." }],
  evidence: { sources: src("tv_holes", ["holeId"]), sample_sizes: { total_holes: 1209 } },
  warnings: ["local LLM unavailable (connection_error); answered with the deterministic template. No cloud model is used."],
  llm: { used: false, model: "llama3.1:8b", error: "connection_error", cloud_fallback: false },
  privacy: { labels: "pseudonymised", min_group_size: 5, raw_records_returned: false },
  elapsed_ms: 42.5, ...over,
});

type Handler = (body: unknown) => { status?: number; json: unknown } | Promise<{ status?: number; json: unknown }>;

/** Route-table fetch mock: keys like "POST /api/chat". Unknown routes return 404. Records every call. */
export function mockFetch(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body: unknown; init: RequestInit }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname, body, init });
    const handler = routes[`${method} ${url.pathname}`];
    const res = handler ? await handler(body) : { status: 404, json: { error: "not_found" } };
    return new Response(JSON.stringify(res.json), { status: res.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

export const statusRoutes = {
  "GET /api/health": () => ({ json: health }),
  "GET /api/ai/status": () => ({ json: aiOffline }),
};
