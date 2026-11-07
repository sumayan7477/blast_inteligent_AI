/**
 * Types mirroring the backend response structures exactly
 * (backend/main.py, chat_service.py, analytics.py, anomaly_detection.py, predictions.py).
 * Optional fields reflect that the backend omits or nulls them depending on mapped data.
 */

export type MetricStatus = "ok" | "unavailable" | "suppressed" | "error";

export interface SourceRef {
  view: string | null;
  dataset?: string | null;
  columns: string[];
}

export interface Metric<D = Record<string, unknown>> {
  key: string;
  title: string;
  status: MetricStatus;
  value: number | string | boolean | null;
  unit: string | null;
  method: string;
  sources: SourceRef[];
  sample_size: number | null;
  /** What `sample_size` counts, e.g. "holes with a valid duration (> 0)". */
  sample_meaning?: string | null;
  missing: Record<string, number>;
  reason: string | null;
  details: D | null;
  limitations?: string[];
}

export interface GroupRow {
  group: string;
  holes: number;
  holes_with_duration?: number;
  median_duration_minutes?: number | null;
  mean_depth?: number | null;
  median_drilling_rate_per_hour?: number | null;
  completion_rate_pct?: number | null;
  problem_flag_rate_pct?: number | null;
  utilization_pct?: number | null;
  utilization_note?: string | null;
  problems_per_100_holes?: number | null;
  blasts?: number | null;
}

export interface RigDetails {
  rigs: GroupRow[];
  rigs_suppressed_small_sample: number;
  labels: "real" | "pseudonymised";
}

export interface SiteDetails {
  sites: GroupRow[];
  sites_suppressed_small_sample: number;
  labels: "real" | "pseudonymised";
}

export interface DurationDetails {
  mean_minutes: number | null;
  median_minutes: number | null;
  p90_minutes: number | null;
  stddev_minutes: number | null;
}

export interface DistributionBucket {
  bucket: string;
  holes: number | null;
  share_pct: number | null;
  suppressed: boolean;
}

export interface ProblemDetails {
  problem_records?: number;
  by_category?: { category: string; records: number; share_pct: number | null }[];
  categories_suppressed?: number;
  per_100_holes_note?: string;
}

export interface DistributionStats {
  blasts_with_value: number;
  mean?: number | null;
  median?: number | null;
  p10?: number | null;
  p90?: number | null;
  suppressed?: boolean;
}

export interface BlastComparisonRow {
  requested_index: number;
  found: boolean;
  blast?: string;
  percentile_vs_all_blasts?: Record<string, number | null>;
  values?: Record<string, number | null>;
}

export interface TrendPoint {
  period: string;
  suppressed?: boolean;
  holes?: number;
  blasts?: number;
  problems?: number;
  median_duration_minutes?: number | null;
  completion_rate_pct?: number | null;
}

export type TrendDetails = Partial<Record<"holes" | "blasts" | "problems", TrendPoint[]>>;

export interface FlaggedRecord {
  record: { _record_key: string; _source_file: string; _source_line: number } | null;
  group?: string | null;
  robust_z?: number;
  percentile?: number;
  isolation_percentile?: number;
  explanation: string;
  label: string;
}

export interface Detector {
  detector: string;
  title: string;
  status: MetricStatus;
  reason: string | null;
  method: string;
  sources: SourceRef[];
  records_scored: number | null;
  records_considered?: number | null;
  groups_scored?: number | null;
  groups_not_scored?: number | null;
  flagged_count: number | null;
  flagged: FlaggedRecord[];
  features_used?: string[];
  features_dropped_missing?: string[];
}

export interface AnomalyResult {
  label: string;
  robust_z_threshold: number;
  detectors: Detector[];
}

export interface ReadinessFeature {
  feature: string;
  kind: "numeric" | "categorical";
  missing_pct: number | null;
  distinct_values: number;
  usable: boolean;
  note: string | null;
}

export interface Readiness {
  ready: boolean;
  target: { name: string; definition: string; columns: string[]; view: string } | null;
  reasons: string[];
  features: ReadinessFeature[];
  excluded: { role: string; column: string; reason: string }[];
  target_quality?: {
    rows: number;
    rows_with_target: number;
    valid_targets: number;
    invalid_targets_non_positive: number;
    missing_target: number;
    distinct_values: number;
    coefficient_of_variation: number | null;
  };
  split_strategy?: string;
}

export type AnalyticsSection =
  | "overview"
  | "drilling"
  | "rigs"
  | "sites"
  | "blasts"
  | "compare-blasts"
  | "trends"
  | "problems"
  | "anomalies"
  | "prediction-readiness";

export interface AnalyticsFilters {
  date_from?: string;
  date_to?: string;
  rigs?: string[];
  sites?: string[];
  blasts?: string[];
  blast_ids?: string[];
  grain?: "month" | "week";
}

export interface FilterDescription {
  date_from: string | null;
  date_to: string | null;
  rig_values: number;
  site_values: number;
  blast_values: number;
  grain?: string;
  blast_ids_requested?: number;
  group_by?: string | null;
}

export interface PrivacyInfo {
  labels: "real" | "pseudonymised";
  min_group_size: number | null;
  raw_records_returned: boolean;
}

export interface AnalyticsResponse {
  section: AnalyticsSection;
  filters: FilterDescription;
  result: { metrics?: Metric[]; anomalies?: AnomalyResult; readiness?: Readiness };
  privacy: PrivacyInfo;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  database: { available: boolean; read_only: boolean; error: string | null };
  analytics_mapping: { available: boolean; confirmed_entities: string[]; error: string | null };
  approved_views: number;
  active_sessions: number;
}

export interface AiStatusResponse {
  provider: string;
  base_url?: string;
  local_only: boolean;
  cloud_fallback: boolean;
  model?: string;
  reachable: boolean;
  model_available: boolean;
  available_models?: string[];
  error: string | null;
  chat_uses_llm: boolean;
  fallback_when_unavailable?: string;
}

export type CountKind = "total_rows" | "filtered_rows" | "non_null" | "distinct" | "sample" | "coverage" | "metric";

export interface ChatFact {
  type: "fact";
  statement: string;
  metric: string | null;
  value: number | string | boolean | null;
  unit: string | null;
  /** What kind of count `value` is (rows, distinct entities, ...). */
  count_kind?: CountKind;
  sample_size: number | null;
  sample_meaning?: string | null;
  sources: SourceRef[];
  method: string | null;
}

export interface ChatAnomaly {
  type: "statistical_anomaly";
  detector: string;
  title: string;
  flagged_count: number | null;
  records_scored: number | null;
  method: string;
  sources: SourceRef[];
  examples: { reference: string; explanation: string }[];
  label: string;
}

export interface ChatUnavailable {
  type: "unavailable";
  metric: string | null;
  title: string;
  status: string | null;
  reason: string;
}

export interface ChatSuggestion {
  type: "suggestion";
  category: "data_mapping" | "review" | string;
  text: string;
}

export interface ChatResponse {
  session_id: string;
  operation: string;
  intent_source: string;
  filters: FilterDescription;
  answer: string;
  answer_source: "llm" | "template";
  facts: ChatFact[];
  statistical_anomalies: ChatAnomaly[];
  unavailable: ChatUnavailable[];
  suggestions: ChatSuggestion[];
  limitations?: { text: string; source?: string | null }[];
  evidence: { sources: SourceRef[]; sample_sizes: Record<string, number> };
  warnings: string[];
  llm: { used: boolean; model: string | null; error: string | null; cloud_fallback: boolean };
  privacy: PrivacyInfo;
  elapsed_ms: number;
}
