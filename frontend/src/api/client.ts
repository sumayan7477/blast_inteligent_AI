/**
 * API client for the LOCAL Blast Intelligence backend.
 *
 * - Base URL from VITE_API_BASE_URL, default http://127.0.0.1:8000.
 * - No cookies/credentials, no caching, no referrer.
 * - Errors are normalised into ApiError with a user-safe message; server stack
 *   traces or raw bodies are never surfaced (the backend does not send them either).
 * - Nothing is logged to the console and nothing is persisted in browser storage.
 */
import type {
  AiStatusResponse,
  AnalyticsFilters,
  AnalyticsResponse,
  AnalyticsSection,
  ChatResponse,
  HealthResponse,
} from "./types";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

export function resolveBaseUrl(raw: string | undefined | null): string {
  const value = (raw ?? "").trim();
  if (!value) return DEFAULT_API_BASE_URL;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return DEFAULT_API_BASE_URL;
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch {
    return DEFAULT_API_BASE_URL;
  }
}

export function isLocalUrl(base: string): boolean {
  try {
    return LOCAL_HOSTS.has(new URL(base).hostname);
  } catch {
    return false;
  }
}

export type ApiErrorKind = "network" | "timeout" | "http" | "invalid_response" | "aborted";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly code?: string;

  constructor(kind: ApiErrorKind, message: string, status?: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
  }

  /** True when the backend says the capability is not available (honest "unavailable" state). */
  get isUnavailable(): boolean {
    return this.status === 503 || this.status === 404 || this.code === "analytics_unavailable";
  }
}

const FRIENDLY: Record<string, string> = {
  analytics_unavailable: "Analytics are unavailable: the database or confirmed analytics mapping is not loaded.",
  unknown_section: "This analytics endpoint is not available in the running backend version.",
  rate_limited: "Too many requests. Please wait a moment and try again.",
  invalid_request: "The request was rejected by the backend validation.",
  invalid_question: "The question was rejected (empty or too long).",
  payload_too_large: "The request is too large.",
  timeout: "The analytics query hit its time limit.",
  analytics_failed: "The analytics query failed on the backend.",
  internal_error: "The backend reported an unexpected error.",
};

function messageFor(status: number, code: string | undefined, detail: unknown): string {
  if (code && FRIENDLY[code]) {
    // Our backend's `detail` strings are short, value-free messages; only use them for client errors.
    if (status < 500 && typeof detail === "string" && detail.length <= 200 && code !== "invalid_request") {
      return `${FRIENDLY[code]} (${detail})`;
    }
    return FRIENDLY[code];
  }
  if (status === 404) return "Endpoint not found on the backend.";
  if (status >= 500) return "The backend reported an error.";
  return `Request failed (HTTP ${status}).`;
}

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
}

export function createApiClient(baseUrl: string, fetchImpl: typeof fetch = (...a) => fetch(...a)) {
  async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const controller = new AbortController();
    const timeoutMs = opts.timeoutMs ?? 60_000;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const onAbort = () => controller.abort();
    opts.signal?.addEventListener("abort", onAbort);
    let res: Response;
    try {
      res = await fetchImpl(`${baseUrl}${path}`, {
        method: opts.method ?? "GET",
        headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
    } catch {
      if (timedOut) throw new ApiError("timeout", "The backend did not respond in time.");
      if (opts.signal?.aborted) throw new ApiError("aborted", "Request cancelled.");
      throw new ApiError("network", `Cannot reach the local backend at ${baseUrl}. Is it running?`);
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
    }
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      if (res.ok) throw new ApiError("invalid_response", "The backend returned an unreadable response.", res.status);
    }
    if (!res.ok) {
      const obj = (data && typeof data === "object" ? data : {}) as { error?: unknown; detail?: unknown };
      const code = typeof obj.error === "string" ? obj.error : undefined;
      throw new ApiError("http", messageFor(res.status, code, obj.detail), res.status, code);
    }
    if (data === null || typeof data !== "object") {
      throw new ApiError("invalid_response", "The backend returned an unexpected response.", res.status);
    }
    return data as T;
  }

  return {
    baseUrl,
    health: (signal?: AbortSignal) => request<HealthResponse>("/api/health", { signal, timeoutMs: 10_000 }),
    aiStatus: (signal?: AbortSignal) => request<AiStatusResponse>("/api/ai/status", { signal, timeoutMs: 15_000 }),
    chat: (question: string, sessionId: string | null, signal?: AbortSignal) =>
      request<ChatResponse>("/api/chat", {
        method: "POST",
        body: sessionId ? { question, session_id: sessionId } : { question },
        signal,
        timeoutMs: 180_000, // local LLM generation can be slow on CPU
      }),
    clearChat: (sessionId: string) =>
      request<{ session_id: string; cleared: boolean }>("/api/chat/clear", {
        method: "POST",
        body: { session_id: sessionId },
        timeoutMs: 10_000,
      }),
    analytics: (section: AnalyticsSection, filters: AnalyticsFilters = {}, signal?: AbortSignal) =>
      request<AnalyticsResponse>(`/api/analytics/${section}`, {
        method: "POST",
        body: cleanFilters(filters),
        signal,
        timeoutMs: 90_000,
      }),
  };
}

/** Drop empty values so the backend receives only meaningful filters. */
export function cleanFilters(f: AnalyticsFilters): AnalyticsFilters {
  const out: AnalyticsFilters = {};
  if (f.date_from) out.date_from = f.date_from;
  if (f.date_to) out.date_to = f.date_to;
  for (const k of ["rigs", "sites", "blasts", "blast_ids"] as const) {
    const vals = (f[k] ?? []).map((v) => v.trim()).filter(Boolean);
    if (vals.length) out[k] = vals;
  }
  if (f.grain) out.grain = f.grain;
  return out;
}

export type ApiClient = ReturnType<typeof createApiClient>;

export const API_BASE_URL = resolveBaseUrl(import.meta.env.VITE_API_BASE_URL as string | undefined);
export const api: ApiClient = createApiClient(API_BASE_URL);
