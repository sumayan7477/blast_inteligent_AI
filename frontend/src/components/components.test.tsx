import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnomalyReview } from "./AnomalyReview";
import { EvidencePanel } from "./EvidencePanel";
import { MetricCard } from "./MetricCard";
import { ReadinessPanel } from "./ReadinessPanel";
import { anomalies, chatResponse, metric, readiness } from "../test/fixtures";

describe("MetricCard", () => {
  it("shows value, sample size, source view and method", () => {
    render(<MetricCard title="Total drilled holes" metric={metric({})} />);
    expect(screen.getByTestId("metric-value")).toHaveTextContent("1,209");
    expect(screen.getByText(/n = 1,209/)).toBeInTheDocument();
    expect(screen.getAllByText("tv_holes").length).toBeGreaterThan(0);
    expect(screen.getByText("COUNT(DISTINCT hole_id) over the holes view")).toBeInTheDocument();
  });

  it("shows the reason for unavailable and suppressed metrics instead of a number", () => {
    render(<MetricCard title="Problems" metric={metric({ status: "unavailable", value: null, reason: "holes.problem_flag not mapped" })} />);
    expect(screen.getByTestId("metric-reason")).toHaveTextContent("holes.problem_flag not mapped");
    expect(screen.queryByTestId("metric-value")).not.toBeInTheDocument();
    expect(screen.getByText("unavailable")).toBeInTheDocument();
  });

  it("is honest when the backend did not return the metric", () => {
    render(<MetricCard title="Completion rate" metric={null} />);
    expect(screen.getByText("Not returned by the backend.")).toBeInTheDocument();
  });
});

describe("EvidencePanel", () => {
  it("renders facts with metric, value, n, method, view and columns", () => {
    render(<EvidencePanel response={chatResponse()} />);
    const fact = screen.getByTestId("fact");
    expect(within(fact).getByText("Total drilled holes: 1,209 holes")).toBeInTheDocument();
    expect(within(fact).getByText("total_holes")).toBeInTheDocument();
    expect(within(fact).getByText("n = 1,209")).toBeInTheDocument();
    expect(within(fact).getByText(/COUNT\(DISTINCT hole_id\)/)).toBeInTheDocument();
    expect(within(fact).getByText("tv_holes")).toBeInTheDocument();
    expect(within(fact).getByText(/holeId/)).toBeInTheDocument();
  });

  it("renders unavailable reasons, suggestions and warnings", () => {
    render(<EvidencePanel response={chatResponse()} />);
    expect(screen.getByTestId("unavailable-item")).toHaveTextContent("no confirmed problems entity");
    expect(screen.getByText(/Map and confirm problems/)).toBeInTheDocument();
    expect(screen.getByText(/No cloud model is used/)).toBeInTheDocument();
  });

  it("labels anomalies as statistical, not confirmed problems", () => {
    const r = chatResponse({
      statistical_anomalies: [{ type: "statistical_anomaly", detector: "hole_duration", title: "Unusual drilling duration",
        flagged_count: 5, records_scored: 1205, method: "robust z", sources: [], label: "Statistical outlier for human review - NOT a confirmed operational problem.",
        examples: [{ reference: "holes.jsonl line 51", explanation: "drilling duration is 9.1 robust SDs above the median" }] }],
    });
    render(<EvidencePanel response={r} />);
    expect(screen.getByRole("note")).toHaveTextContent(/NOT a confirmed operational problem/);
    expect(screen.getByTestId("anomaly")).toHaveTextContent("5 flagged / 1,205");
    expect(screen.getByText("holes.jsonl line 51")).toBeInTheDocument();
  });

  it("labels count types, what n counts, and shows limitations", () => {
    const r = chatResponse({
      facts: [
        { type: "fact", statement: "Drilled holes (distinct hole IDs): 2,266 distinct hole IDs", metric: "total_holes",
          value: 2266, unit: "distinct hole IDs", count_kind: "distinct", sample_size: 288434,
          sample_meaning: "hole records with a non-null hole ID", sources: [], method: "COUNT(DISTINCT hole_id)" },
        { type: "fact", statement: "Hole records in tv_holes: 288,434", metric: "total_holes", value: 288434,
          unit: "records", count_kind: "total_rows", sample_size: 288434, sample_meaning: "all rows in the view",
          sources: [], method: "COUNT(*)" },
      ],
      limitations: [{ text: "Hole IDs repeat across records (on average 127.3 records per ID)", source: "total_holes" }],
    });
    render(<EvidencePanel response={r} />);
    const [distinct, rows] = screen.getAllByTestId("fact");
    expect(within(distinct).getByText("Distinct entities")).toBeInTheDocument();
    expect(within(distinct).getByText(/hole records with a non-null hole ID/)).toBeInTheDocument();
    expect(within(rows).getByText("Total records (all rows)")).toBeInTheDocument();
    expect(screen.getByTestId("limitation")).toHaveTextContent("127.3 records per ID");
  });

  it("has an empty state", () => {
    render(<EvidencePanel response={null} />);
    expect(screen.getByText(/Ask a question to see the evidence/)).toBeInTheDocument();
  });
});

describe("AnomalyReview and ReadinessPanel", () => {
  it("shows detectors, flagged references and unavailable detectors in detailed mode", () => {
    render(<AnomalyReview result={anomalies.result.anomalies!} detailed />);
    expect(screen.getByText("holes.jsonl:51")).toBeInTheDocument();
    expect(screen.getByText(/needs >= 500 hole records/)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/NOT a confirmed/);
  });

  it("shows not-ready reasons, features and leakage exclusions", () => {
    render(<ReadinessPanel readiness={readiness.result.readiness!} detailed />);
    expect(screen.getByText("Not ready for prediction")).toBeInTheDocument();
    expect(screen.getByText(/only 120 valid duration targets/)).toBeInTheDocument();
    expect(screen.getByText("more than 50% missing")).toBeInTheDocument();
    expect(screen.getByText(/known only after drilling finishes/)).toBeInTheDocument();
  });
});
