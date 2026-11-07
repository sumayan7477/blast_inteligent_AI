import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
import { AssistantPage } from "./AssistantPage";
import { DashboardPage } from "./DashboardPage";
import { AnalyticsPage } from "./AnalyticsPage";
import { aiOffline, anomalies, chatResponse, mockFetch, overview, readiness, rigs, statusRoutes } from "../test/fixtures";

const analyticsRoutes = {
  "POST /api/analytics/overview": () => ({ json: overview }),
  "POST /api/analytics/rigs": () => ({ json: rigs }),
  "POST /api/analytics/anomalies": () => ({ json: anomalies }),
  "POST /api/analytics/prediction-readiness": () => ({ json: readiness }),
  "POST /api/analytics/drilling": () => ({ status: 503, json: { error: "analytics_unavailable", detail: "x" } }),
  "POST /api/analytics/trends": () => ({ status: 500, json: { error: "internal_error", detail: "unexpected server error" } }),
};

describe("Dashboard", () => {
  it("shows loading, then backend KPIs with evidence and honest unavailable/error states", async () => {
    mockFetch(analyticsRoutes);
    render(<DashboardPage />);
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);              // loading
    const cards = await screen.findAllByTestId("metric-card");
    expect(within(cards[0]).getByTestId("metric-value")).toHaveTextContent("40");   // total blasts from backend
    expect(within(cards[2]).getByTestId("metric-value")).toHaveTextContent("89.53");
    expect(within(cards[3]).getByTestId("metric-reason")).toHaveTextContent(/no confirmed problems entity/);
    expect(await screen.findAllByTestId("unavailable")).not.toHaveLength(0);       // drilling 503
    expect(await screen.findByText("The backend reported an unexpected error.")).toBeInTheDocument(); // trends 500
    expect(screen.queryByText(/Traceback/)).not.toBeInTheDocument();
    expect(await screen.findByText(/Unusual drilling duration/)).toBeInTheDocument();
    expect(await screen.findByText("Not ready for prediction")).toBeInTheDocument();
  });

  it("sends date filters in the POST body, never in the URL", async () => {
    const { calls } = mockFetch(analyticsRoutes);
    render(<DashboardPage />);
    await screen.findAllByTestId("metric-card");
    await userEvent.type(screen.getByLabelText("From date"), "2024-02-01");
    await userEvent.click(screen.getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(calls.some((c) => (c.body as { date_from?: string })?.date_from === "2024-02-01")).toBe(true));
    expect(calls.every((c) => !c.path.includes("2024"))).toBe(true);
  });
});

describe("AI Assistant", () => {
  it("shows the empty state with suggested questions and LLM status", () => {
    mockFetch({});
    render(<AssistantPage ai={aiOffline} />);
    expect(screen.getByTestId("chat-empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Compare rig performance" })).toBeInTheDocument();
    expect(screen.getByText(/Ollama offline · template answers/)).toBeInTheDocument();
  });

  it("sends questions, keeps the session for follow-ups, shows evidence and clears", async () => {
    let n = 0;
    const { calls } = mockFetch({
      "POST /api/chat": () => ({ json: chatResponse({ answer: `Answer ${++n}` }) }),
      "POST /api/chat/clear": () => ({ json: { session_id: "sess_synthetic_0001", cleared: true } }),
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    render(<AssistantPage ai={aiOffline} />);
    const box = screen.getByLabelText("Question");

    await userEvent.type(box, "How many holes were drilled?{Enter}");
    expect(await screen.findByText("Answer 1")).toBeInTheDocument();
    expect(calls[0].body).toEqual({ question: "How many holes were drilled?" });
    expect(screen.getByText("Answer: deterministic template")).toBeInTheDocument();
    expect(screen.getByTestId("answer-meta").textContent).toMatch(/deterministic template\s+Topic: Overview/);
    expect(screen.getByTestId("evidence-panel")).toHaveTextContent("Total drilled holes: 1,209 holes");

    await userEvent.type(box, "What about February 2024?{Enter}");
    expect(await screen.findByText("Answer 2")).toBeInTheDocument();
    expect(calls[1].body).toEqual({ question: "What about February 2024?", session_id: "sess_synthetic_0001" });

    await userEvent.click(screen.getByRole("button", { name: /clear/i }));
    await waitFor(() => expect(calls.at(-1)).toMatchObject({ path: "/api/chat/clear", body: { session_id: "sess_synthetic_0001" } }));
    expect(screen.getByTestId("chat-empty")).toBeInTheDocument();
    expect(setItem).not.toHaveBeenCalled();                                   // nothing persisted in browser storage
  });

  it("shows an LLM answer badge and the statistical-anomaly distinction", async () => {
    mockFetch({
      "POST /api/chat": () => ({ json: chatResponse({
        answer_source: "llm", llm: { used: true, model: "llama3.1:8b", error: null, cloud_fallback: false }, warnings: [],
        statistical_anomalies: [{ type: "statistical_anomaly", detector: "hole_duration", title: "Unusual drilling duration",
          flagged_count: 5, records_scored: 1205, method: "robust z", sources: [], examples: [], label: "NOT a confirmed operational problem." }],
      }) }),
    });
    render(<AssistantPage ai={{ ...aiOffline, reachable: true, model_available: true, error: null }} />);
    await userEvent.click(screen.getByRole("button", { name: "Were there any unusual drilling durations?" }));
    expect(await screen.findByText(/Answer: local LLM \(llama3\.1:8b\)/)).toBeInTheDocument();
    expect(screen.getByText(/5 statistical outlier\(s\) flagged for review — not confirmed operational problems/)).toBeInTheDocument();
  });

  it("shows a safe error with retry and a loading state", async () => {
    let fail = true;
    let release: () => void = () => undefined;
    mockFetch({
      "POST /api/chat": () => fail
        ? { status: 500, json: { error: "internal_error", detail: "boom stack" } }
        : new Promise((resolve) => { release = () => resolve({ json: chatResponse({ answer: "Recovered" }) }); }),
    });
    render(<AssistantPage ai={aiOffline} />);
    await userEvent.type(screen.getByLabelText("Question"), "How many blasts?{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("The backend reported an unexpected error.");
    expect(screen.queryByText(/boom stack/)).not.toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: /retry question/i }));
    expect(await screen.findByText(/Querying the local database/)).toBeInTheDocument();
    await act(async () => release());
    expect(await screen.findByText("Recovered")).toBeInTheDocument();
  });

  it("renders unsupported answers readably, never as glued labels", async () => {
    mockFetch({
      "POST /api/chat": () => ({ json: chatResponse({
        operation: "unsupported", facts: [], unavailable: [], suggestions: [], warnings: [], limitations: [],
        answer: "I can't answer that question from the database yet.\nI can answer questions about:\n- available datasets, record counts and data coverage",
      }) }),
    });
    render(<AssistantPage ai={aiOffline} />);
    await userEvent.type(screen.getByLabelText("Question"), "Write a poem{Enter}");
    expect(await screen.findByText(/I can't answer that question from the database yet/)).toBeInTheDocument();
    const meta = screen.getByTestId("answer-meta").textContent ?? "";
    expect(meta).toContain("Topic: Not supported");
    expect(meta).not.toMatch(/templateunsupported/i);
  });

  it("enforces the question length limit", async () => {
    mockFetch({});
    render(<AssistantPage ai={aiOffline} />);
    const box = screen.getByLabelText("Question");
    await userEvent.click(box);
    await userEvent.paste("x".repeat(1001));
    expect(screen.getByRole("button", { name: /ask/i })).toBeDisabled();
    expect(screen.getByText("1001/1000")).toBeInTheDocument();
  });
});

describe("Analytics", () => {
  it("renders rig comparison with pseudonymised labels and suppression note", async () => {
    mockFetch(analyticsRoutes);
    render(<AnalyticsPage tab="rigs" />);
    expect(await screen.findByText("RIG-aaa111")).toBeInTheDocument();
    expect(screen.getByText(/1 group\(s\) suppressed for privacy/)).toBeInTheDocument();
    expect(screen.getByText("pseudonymised labels")).toBeInTheDocument();
  });

  it("compares blasts only after ids are submitted, sending them in the body", async () => {
    const { calls } = mockFetch({
      ...analyticsRoutes,
      "POST /api/analytics/blasts": () => ({ status: 503, json: { error: "analytics_unavailable" } }),
      "POST /api/analytics/compare-blasts": () => ({ json: { ...overview, section: "compare-blasts", result: { metrics: [{
        key: "blast_comparison", title: "Blast comparison", status: "ok", value: 1, unit: "blasts compared", method: "m", sources: [],
        sample_size: 40, missing: {}, reason: null,
        details: { blasts: [{ requested_index: 0, found: true, blast: "BLAST-c0ffee", percentile_vs_all_blasts: { holes: 72.5 }, values: { holes: 31 } },
                            { requested_index: 1, found: false }], not_found: 1 } }] } } }),
    });
    render(<AnalyticsPage tab="blasts" />);
    expect(calls.some((c) => c.path.endsWith("compare-blasts"))).toBe(false);
    await userEvent.type(screen.getByPlaceholderText(/Blast ids/), "B-1, B-2");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByText("BLAST-c0ffee")).toBeInTheDocument();
    expect(screen.getByText("not found")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("compare-blasts"))?.body).toEqual({ blast_ids: ["B-1", "B-2"] });
  });

  it("shows the anomaly review and prediction details", async () => {
    mockFetch(analyticsRoutes);
    const { unmount } = render(<AnalyticsPage tab="anomalies" />);
    expect(await screen.findByText("holes.jsonl:51")).toBeInTheDocument();
    unmount();
    render(<AnalyticsPage tab="prediction" />);
    expect(await screen.findByText(/known only after drilling finishes/)).toBeInTheDocument();
  });
});

describe("App shell", () => {
  it("shows backend unreachable and LLM offline status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    render(<App />);
    // Shell banner + each dashboard panel report the same network error.
    expect((await screen.findAllByText(/Cannot reach the local backend/)).length).toBeGreaterThan(1);
    expect(screen.getByText(/python backend\\main\.py/)).toBeInTheDocument();
    expect(screen.getAllByText(/unreachable/).length).toBeGreaterThan(0);
  });

  it("navigates between pages via the hash router", async () => {
    mockFetch({ ...statusRoutes, ...analyticsRoutes });
    render(<App />);
    await userEvent.click(screen.getAllByRole("button", { name: /AI Assistant/ })[0]);
    expect(await screen.findByTestId("chat-empty")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/assistant");
  });
});
