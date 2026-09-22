import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { ProbeCandidate, ProbeReport } from "../lib/api";
import { RetrievalProbe } from "./RetrievalProbe";

/**
 * Renders the panel with `api.exploreProbe` replaced. What is asserted is the
 * contract the screen exists for: the sandbox sentence is always on screen,
 * the chunk whose context is open is what gets placed, a candidate row carries
 * its three legs rather than one fused number, the verdict names one gate,
 * the unrecorded spend is printed, and a failed probe leaves a panel that can
 * be retried — the recorded `deciding` latch, on a new screen.
 */
const { exploreProbe } = vi.hoisted(() => ({ exploreProbe: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, exploreProbe } };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

function candidate(over: Partial<ProbeCandidate> = {}): ProbeCandidate {
  return {
    chunkId: "chk_" + "a".repeat(24),
    source: "01 · El Reto de Dios",
    breadcrumb: "Capítulo 11",
    kind: "cuerpo",
    preview: "Tras sufrir el ataque…",
    rrfRank: 1,
    rank: 1,
    denseRank: 1,
    denseScore: 0.703,
    sparseRank: 2,
    sparseScore: 14.9,
    rerankScore: 0.881,
    deliveredRank: 1,
    ...over,
  };
}

function report(over: Partial<ProbeReport> = {}): ProbeReport {
  return {
    question: "¿Cómo reaccionó la congregación?",
    effort: "brief",
    queryTerms: ["reacciono", "congregacion"],
    onTopic: true,
    denseSupported: 4,
    servedWith: {
      minScore: 0.6, prefetchLimit: 50, candidateLimit: 20, perSection: 2, topK: 4,
      effort: "brief", reranked: true, rerankModel: "semantic-ranker-default-005",
      rerankNote: "", depth: 500,
    },
    candidates: [
      candidate(),
      candidate({ chunkId: "chk_" + "b".repeat(24), rank: 2, rrfRank: 5, deliveredRank: null,
                  denseRank: 7, denseScore: 0.61, sparseRank: null, sparseScore: null,
                  rerankScore: 0.42, breadcrumb: "Capítulo 3" }),
    ],
    target: null,
    spent: { embeddingInputTokens: 0, cacheHits: 1, rerankUsd: 0.001, recorded: false },
    sandbox: true,
    ...over,
  };
}

beforeEach(async () => {
  exploreProbe.mockReset();
  await i18n.changeLanguage("en");
});
afterEach(cleanup);

async function probe(question = "¿qué pasó?") {
  fireEvent.change(screen.getByPlaceholderText(t("probe.placeholder")), {
    target: { value: question },
  });
  fireEvent.click(screen.getByRole("button", { name: t("probe.run") }));
  await waitFor(() => expect(exploreProbe).toHaveBeenCalled());
}

describe("RetrievalProbe", () => {
  it("says it is a sandbox before anything is typed", () => {
    render(<RetrievalProbe libraryId="lib_a" versionId={null} chunkId={null} />);
    expect(screen.getByText(t("probe.sandbox"))).toBeTruthy();
    expect(screen.getByRole("button", { name: t("probe.run") }).hasAttribute("disabled")).toBe(true);
  });

  it("places the chunk whose context is open, and narrows only when asked", async () => {
    exploreProbe.mockResolvedValue(report());
    render(
      <RetrievalProbe libraryId="lib_a" versionId="ver_x" chunkId={"chk_" + "c".repeat(24)} />,
    );
    await probe("¿qué pasó?");
    expect(exploreProbe).toHaveBeenCalledWith({
      libraryId: "lib_a",
      question: "¿qué pasó?",
      effort: "standard",
      chunkId: "chk_" + "c".repeat(24),
      versionId: undefined,
    });
  });

  it("sends no chunk when none is open, and the narrowing when ticked", async () => {
    exploreProbe.mockResolvedValue(report());
    render(<RetrievalProbe libraryId="lib_a" versionId="ver_x" chunkId={null} />);
    fireEvent.click(screen.getByLabelText(t("probe.narrow")));
    await probe();
    const args = exploreProbe.mock.calls[0]![0];
    expect(args.chunkId).toBeUndefined();
    expect(args.versionId).toBe("ver_x");
  });

  it("carries the chosen level, never a number", async () => {
    exploreProbe.mockResolvedValue(report());
    render(<RetrievalProbe libraryId="lib_a" versionId={null} chunkId={null} />);
    fireEvent.click(screen.getByLabelText(t("ask.effort.brief")));
    await probe();
    expect(exploreProbe.mock.calls[0]![0].effort).toBe("brief");
    expect(JSON.stringify(exploreProbe.mock.calls[0]![0])).not.toMatch(/topK|top_k|candidate/);
  });

  it("shows every candidate with its legs pulled apart, and marks the dropped one", async () => {
    exploreProbe.mockResolvedValue(report());
    const { container } = render(
      <RetrievalProbe libraryId="lib_a" versionId={null} chunkId={null} />,
    );
    await probe();
    const rows = container.querySelectorAll("table.candidates tbody tr");
    expect(rows.length).toBe(2);
    const [first, second] = [rows[0]!, rows[1]!];
    expect(first.className).toBe("delivered");
    expect(first.textContent).toContain("0.703");
    expect(first.textContent).toContain("14.90");
    expect(first.textContent).toContain("0.881");
    expect(second.className).toBe("dropped");
    expect(second.textContent).toContain(t("probe.dropped"));
    // A leg that never ranked it is a dash, never a zero.
    expect(second.textContent).toContain("— · —");
    expect(screen.getByText(t("probe.reranked", { model: "semantic-ranker-default-005" }), { exact: false })).toBeTruthy();
  });

  it("names the one gate a placed chunk was lost at", async () => {
    exploreProbe.mockResolvedValue(
      report({
        target: {
          target: "chk_" + "c".repeat(24),
          singleTermQuery: false,
          dense: { leg: "dense", rank: 496, score: 0.4967, searched: 500, prefetchLimit: 50,
                   floor: 0.6, clearsFloor: false, inPrefetch: false },
          sparse: { leg: "sparse", rank: null, score: null, searched: 25, prefetchLimit: 50,
                    floor: null, clearsFloor: null, inPrefetch: false },
          fusedRank: null, rrfRank: null, rerankScore: null,
          verdict: { reached: false, lostAt: "prefetch", deliveredRank: null,
                     detail: "dense: 0.4967 < floor 0.6", remedy: "the floor excludes it",
                     note: "RRF never saw this chunk" },
          note: null,
        },
      }),
    );
    render(<RetrievalProbe libraryId="lib_a" versionId={null} chunkId={"chk_" + "c".repeat(24)} />);
    await probe();
    expect(screen.getByText(t("probe.lostAt", { gate: t("probe.gate.prefetch") }))).toBeTruthy();
    expect(screen.getByText("the floor excludes it")).toBeTruthy();
    // "below where we looked" renders as a window, never as a rank of 0.
    expect(screen.getByText("> 25")).toBeTruthy();
    expect(screen.getByText("496~ / 500")).toBeTruthy();
  });

  it("prints what the probe spent and that nothing was recorded", async () => {
    exploreProbe.mockResolvedValue(report());
    render(<RetrievalProbe libraryId="lib_a" versionId={null} chunkId={null} />);
    await probe();
    expect(
      screen.getByText(t("probe.spent", { tokens: 0, hits: 1, usd: "0.0010" })),
    ).toBeTruthy();
  });

  it("clicking a candidate hands its chunk to the caller", async () => {
    exploreProbe.mockResolvedValue(report());
    const onPick = vi.fn();
    render(<RetrievalProbe libraryId="lib_a" versionId={null} chunkId={null} onPick={onPick} />);
    await probe();
    fireEvent.click(screen.getAllByRole("button", { name: "01 · El Reto de Dios" })[0]!);
    expect(onPick).toHaveBeenCalledWith("chk_" + "a".repeat(24));
  });

  it("a failed probe shows the guidance and leaves the button answerable", async () => {
    // The shape Rust hands the webview for a control-plane refusal: the kind
    // is `control_status` and the plane's own kind rides in `controlKind`.
    exploreProbe.mockRejectedValue({
      kind: "control_status", controlKind: "qdrant_unreachable", message: "503: Qdrant no responde",
    });
    render(<RetrievalProbe libraryId="lib_a" versionId={null} chunkId={null} />);
    await probe();
    await waitFor(() => expect(screen.getByText(t("error.qdrantUnreachable"))).toBeTruthy());
    expect(screen.getByRole("button", { name: t("probe.run") }).hasAttribute("disabled")).toBe(false);
  });
});
