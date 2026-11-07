import { describe, expect, it, vi } from "vitest";
import { ApiError, DEFAULT_API_BASE_URL, cleanFilters, createApiClient, isLocalUrl, resolveBaseUrl } from "./client";

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("base URL", () => {
  it("defaults to the local backend and normalises values", () => {
    expect(resolveBaseUrl(undefined)).toBe(DEFAULT_API_BASE_URL);
    expect(resolveBaseUrl("")).toBe("http://127.0.0.1:8000");
    expect(resolveBaseUrl("http://localhost:9000/")).toBe("http://localhost:9000");
    expect(resolveBaseUrl("javascript:alert(1)")).toBe(DEFAULT_API_BASE_URL);
    expect(resolveBaseUrl("not a url")).toBe(DEFAULT_API_BASE_URL);
  });
  it("detects non-local hosts", () => {
    expect(isLocalUrl("http://127.0.0.1:8000")).toBe(true);
    expect(isLocalUrl("http://localhost:8000")).toBe(true);
    expect(isLocalUrl("https://api.example.com")).toBe(false);
  });
});

describe("request handling", () => {
  it("sends JSON without credentials or caching", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(200, { session_id: "s", answer: "x" }));
    const client = createApiClient("http://127.0.0.1:8000", fetchImpl as unknown as typeof fetch);
    await client.chat("How many holes?", "abcd1234");
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/api/chat");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
    expect(JSON.parse(String(init.body))).toEqual({ question: "How many holes?", session_id: "abcd1234" });
  });

  it("maps backend error codes to safe messages and never exposes raw bodies", async () => {
    const client = createApiClient("http://x", (async () =>
      jsonResponse(500, { error: "internal_error", detail: "Traceback (most recent call last): secret" })) as unknown as typeof fetch);
    const err = await client.health().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.message).not.toMatch(/Traceback|secret/);
  });

  it("treats 503 analytics_unavailable as an honest unavailable state", async () => {
    const client = createApiClient("http://x", (async () =>
      jsonResponse(503, { error: "analytics_unavailable", detail: "database or confirmed analytics mapping is not available" })) as unknown as typeof fetch);
    const err: ApiError = await client.analytics("overview").catch((e) => e);
    expect(err.isUnavailable).toBe(true);
    expect(err.message).toMatch(/unavailable/i);
  });

  it("reports network failures and timeouts", async () => {
    const down = createApiClient("http://127.0.0.1:1", (async () => { throw new TypeError("Failed to fetch"); }) as unknown as typeof fetch);
    await expect(down.health()).rejects.toMatchObject({ kind: "network" });
    vi.useFakeTimers();
    const slow = createApiClient("http://x", ((_: unknown, init: RequestInit) =>
      new Promise((_r, reject) => init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))))) as unknown as typeof fetch);
    const p = slow.health().catch((e) => e);
    await vi.advanceTimersByTimeAsync(11_000);
    expect(await p).toMatchObject({ kind: "timeout" });
    vi.useRealTimers();
  });
});

describe("cleanFilters", () => {
  it("drops empty values", () => {
    expect(cleanFilters({ date_from: "", rigs: [" ", "R-1 "], sites: [], grain: "week" })).toEqual({ rigs: ["R-1"], grain: "week" });
  });
});
