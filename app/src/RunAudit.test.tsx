import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import i18n from "./i18n";
import type { AuditStage, RunAudit as Audit, RunEventPage } from "./lib/api";
import { RunAudit } from "./RunAudit";

/**
 * The ledger, as a person reads it.
 *
 * The properties worth pinning are the ones the payload was shaped around, and
 * they are all about *not asserting more than is known*: a free stage must not
 * read as `$0.00`, a run Temporal has forgotten must not read as a run that did
 * nothing, and the raw log must not be fetched until somebody asks for it — it
 * is the one call here that can genuinely be slow.
 */
const { runAudit, runEvents, artifactSave } = vi.hoisted(() => ({
  runAudit: vi.fn(),
  runEvents: vi.fn(),
  artifactSave: vi.fn(),
}));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, runAudit, runEvents, artifactSave },
  };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

function stage(over: Partial<AuditStage> = {}): AuditStage {
  return {
    seq: 1,
    stage: "correcting",
    at: "2026-09-01T14:00:00Z",
    endedAt: "2026-09-01T14:04:00Z",
    seconds: 240,
    outcome: null,
    detail: null,
    cost: {
      inputTokens: 1000,
      outputTokens: 500,
      usd: 0.0334,
      unpricedEntries: 0,
      entries: [
        {
          stage: "correction",
          provider: "vertex",
          model: "gemini-3.6-flash",
          inputTokens: 1000,
          outputTokens: 500,
          usd: 0.0334,
        },
      ],
    },
    artifacts: [
      {
        name: "corrected_text",
        relPath: "runs/x/corrected.txt",
        sha256: "aa",
        sizeBytes: 10,
      },
    ],
    ...over,
  };
}

function audit(over: Partial<Audit> = {}): Audit {
  return {
    run: {
      id: "ingest-1",
      workflowId: "ingest-1",
      kind: "index",
      state: "succeeded",
      stage: "done",
      startedAt: "2026-09-01T14:00:00Z",
      finishedAt: "2026-09-01T14:10:00Z",
      errorKind: null,
      errorDetail: null,
      title: "Institución",
      libraryId: "lib_teologia",
      label: null,
      documentId: "doc_1",
      versionId: "ver_1",
    },
    stages: [stage()],
    totals: { inputTokens: 1000, outputTokens: 500, usd: 0.0334, unpricedEntries: 0 },
    warnings: [],
    ...over,
  };
}

const events = (over: Partial<RunEventPage> = {}): RunEventPage => ({
  available: true,
  truncated: false,
  events: [],
  ...over,
});

beforeEach(async () => {
  runAudit.mockReset();
  runEvents.mockReset();
  artifactSave.mockReset();
  await i18n.changeLanguage("es");
});

// Vitest registers no global `afterEach` here, so Testing Library's automatic
// cleanup never runs and each file calls it by hand.
afterEach(cleanup);

it("shows every stage with what it took and what it cost", async () => {
  runAudit.mockResolvedValue(audit());
  render(<RunAudit runId="ingest-1" />);

  await waitFor(() => expect(screen.getByText(t("audit.stage.correcting"))).toBeTruthy());
  expect(screen.getByText("4 min 0 s")).toBeTruthy();
  expect(screen.getAllByText("$0.033400").length).toBeGreaterThan(0);
  expect(screen.getByText("corrected_text")).toBeTruthy();
});

it("says nothing at all about a stage that spends nothing", async () => {
  // The distinction the whole payload is built around. "This stage does not
  // spend" and "this stage's charge was not recorded" are different claims, and
  // `$0.00` renders as the first while sometimes meaning the second.
  runAudit.mockResolvedValue(
    audit({ stages: [stage({ cost: null, artifacts: [] })], totals: {
      inputTokens: 0, outputTokens: 0, usd: null, unpricedEntries: 0,
    } }),
  );
  const { container } = render(<RunAudit runId="ingest-1" />);
  await waitFor(() => expect(screen.getByText(t("audit.stage.correcting"))).toBeTruthy());
  expect(container.textContent).not.toContain("$0.00");
});

it("names the stage a failed run died in", async () => {
  // "It failed" is half an answer; "it failed in `semantics`" is the whole one,
  // and it is the half `run.state` cannot hold.
  runAudit.mockResolvedValue(
    audit({
      run: { ...audit().run, state: "failed", errorDetail: "503 del proveedor" },
      stages: [
        stage({ seq: 1, stage: "semantics", cost: null, artifacts: [] }),
        stage({
          seq: 2, stage: "semantics", endedAt: null, seconds: null,
          outcome: "failed", detail: "activity_failed", cost: null, artifacts: [],
        }),
      ],
    }),
  );
  render(<RunAudit runId="ingest-1" />);
  await waitFor(() => expect(screen.getByText(t("audit.outcome.failed"))).toBeTruthy());
  expect(screen.getByText("503 del proveedor")).toBeTruthy();
  expect(screen.getByText("activity_failed")).toBeTruthy();
});

it("does not fetch the raw log until somebody asks for it", async () => {
  runAudit.mockResolvedValue(audit());
  runEvents.mockResolvedValue(events());
  render(<RunAudit runId="ingest-1" />);

  await waitFor(() => expect(screen.getByText(t("audit.showLog"))).toBeTruthy());
  expect(runEvents).not.toHaveBeenCalled();

  fireEvent.click(screen.getByText(t("audit.showLog")));
  await waitFor(() => expect(runEvents).toHaveBeenCalledWith("ingest-1"));
});

it("says the history aged out rather than showing an empty log", async () => {
  // "Temporal has forgotten this run" and "this run did nothing" must not look
  // the same. The stages above come from the catalog and are unaffected, which
  // is the sentence the panel has to be able to say.
  runAudit.mockResolvedValue(audit());
  runEvents.mockResolvedValue(events({ available: false }));
  render(<RunAudit runId="ingest-1" />);

  await waitFor(() => expect(screen.getByText(t("audit.showLog"))).toBeTruthy());
  fireEvent.click(screen.getByText(t("audit.showLog")));
  await waitFor(() => expect(screen.getByText(t("audit.logExpired"))).toBeTruthy());
  // And the ledger is still there.
  expect(screen.getByText(t("audit.stage.correcting"))).toBeTruthy();
});

it("marks a retry, which is invisible in every other view of a run", async () => {
  runAudit.mockResolvedValue(audit());
  runEvents.mockResolvedValue(
    events({
      events: [
        { id: 11, at: "2026-09-01T14:02:11Z", kind: "ActivityTaskStarted",
          activity: "extract_semantics", attempt: 1, detail: null },
        { id: 42, at: "2026-09-01T14:07:25Z", kind: "ActivityTaskStarted",
          activity: "extract_semantics", attempt: 2, detail: null },
      ],
    }),
  );
  render(<RunAudit runId="ingest-1" />);
  await waitFor(() => expect(screen.getByText(t("audit.showLog"))).toBeTruthy());
  fireEvent.click(screen.getByText(t("audit.showLog")));

  await waitFor(() => expect(screen.getByText(`· ${t("audit.attempt", { n: 2 })}`)).toBeTruthy());
  // The first attempt carries no badge: every run has one, so marking it would
  // make the one that matters harder to see.
  expect(screen.queryByText(`· ${t("audit.attempt", { n: 1 })}`)).toBeNull();
});

it("says a run predating the trail has none, rather than rendering blank", async () => {
  runAudit.mockResolvedValue(audit({ stages: [] }));
  render(<RunAudit runId="ingest-1" />);
  await waitFor(() => expect(screen.getByText(t("audit.noTrail"))).toBeTruthy());
});

it("names the charges no stage claims rather than dropping them", async () => {
  // A ledger that dropped one would be a bill that does not add up. The three a
  // question makes belong to no pipeline stage.
  runAudit.mockResolvedValue(
    audit({
      stages: [
        stage(),
        stage({
          seq: null, stage: null, at: null, endedAt: null, seconds: null,
          artifacts: [],
          cost: {
            inputTokens: 200, outputTokens: 100, usd: 0.0102, unpricedEntries: 0,
            entries: [{ stage: "answering", provider: "vertex", model: "m",
                        inputTokens: 200, outputTokens: 100, usd: 0.0102 }],
          },
        }),
      ],
      totals: { inputTokens: 1200, outputTokens: 600, usd: 0.0436, unpricedEntries: 0 },
    }),
  );
  render(<RunAudit runId="ingest-1" />);
  await waitFor(() => expect(screen.getByText(t("audit.unattributed"))).toBeTruthy());
  expect(screen.getByText("$0.043600")).toBeTruthy();
});

it("offers the book as a button and every other artifact as a label", async () => {
  // A book is the one artifact a person reads rather than a stage. Everything
  // else is a working file, and the allowlist that decides which is which lives
  // on the server — so a button offered for anything else would be a 404 a
  // person had to press to discover.
  runAudit.mockResolvedValue(
    audit({
      stages: [
        stage({
          stage: "epub",
          cost: null,
          artifacts: [
            { name: "epub", relPath: "runs/x/book.epub", sha256: "bb", sizeBytes: 9000 },
            { name: "chunks", relPath: "runs/x/chunks.jsonl", sha256: "cc", sizeBytes: 10 },
          ],
        }),
      ],
    }),
  );
  render(<RunAudit runId="ingest-1" />);

  const book = await screen.findByRole("button", { name: "epub" });
  expect(book).toBeTruthy();
  expect(screen.queryByRole("button", { name: "chunks" })).toBeNull();
});

it("names the saved file after the document, not after the run", async () => {
  // `title` is the document's. A run that failed before registering one has
  // none, which is why the queue's own rule is `title ?? label ?? workflowId` —
  // the file must never be called `ingest-1787….epub`.
  artifactSave.mockResolvedValue("/home/alguien/Institución.epub");
  runAudit.mockResolvedValue(
    audit({
      stages: [
        stage({
          stage: "epub",
          cost: null,
          artifacts: [
            { name: "epub", relPath: "runs/x/book.epub", sha256: "bb", sizeBytes: 9000 },
          ],
        }),
      ],
    }),
  );
  render(<RunAudit runId="ingest-1" />);

  fireEvent.click(await screen.findByRole("button", { name: "epub" }));
  await waitFor(() =>
    expect(artifactSave).toHaveBeenCalledWith("ingest-1", "epub", "Institución.epub"),
  );
  await screen.findByText(
    t("library.downloadedEpub", { path: "/home/alguien/Institución.epub" }),
  );
});

it("says nothing at all when the save dialog is dismissed", async () => {
  // `artifactSave` resolves to null when the person closes the chooser. That is
  // the ordinary way out of a file dialog, and reporting it would put a notice
  // on screen for something nobody did.
  artifactSave.mockResolvedValue(null);
  runAudit.mockResolvedValue(
    audit({
      stages: [
        stage({
          stage: "epub",
          cost: null,
          artifacts: [
            { name: "epub", relPath: "runs/x/book.epub", sha256: "bb", sizeBytes: 9000 },
          ],
        }),
      ],
    }),
  );
  const { container } = render(<RunAudit runId="ingest-1" />);

  fireEvent.click(await screen.findByRole("button", { name: "epub" }));
  await waitFor(() => expect(artifactSave).toHaveBeenCalled());
  expect(container.querySelector(".notice")).toBeNull();
});
