/**
 * What the envelope cache asks for, and what it refuses to ask for twice.
 *
 * The properties here are the ones the screen's own tests can no longer see now
 * that the fetch left the component: that a threshold is never a request, that
 * a floor is one and only the first time, that the graph stays off the critical
 * path of a cold launch, and that one plane's library can never be served to
 * another.
 *
 * Tested as a hook rather than through a rendered screen, for the reason
 * `importQueue.test.ts` gives about its own: the decisions are about data.
 */
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { LibraryGraph } from "./api";
import { BackendProvider } from "./backend";
import { clearGraphCache, graphKey, useLibraryGraph } from "./libraryGraphStore";

const { libraryGraph, backendInfo } = vi.hoisted(() => ({
  libraryGraph: vi.fn(),
  backendInfo: vi.fn(),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, libraryGraph, backendInfo } };
});

function envelope(over: Partial<LibraryGraph> = {}): LibraryGraph {
  return {
    libraryId: "lib_a",
    semantic: true,
    confidenceFloor: 0.6,
    minDocuments: 1,
    documents: [{ documentId: "doc_1", versionId: "ver_1", title: "Historia", format: "pdf" }],
    concepts: [
      { id: "con_a", name: "Gracia", conceptType: null, mentions: 4, documents: 4 },
      { id: "con_b", name: "Concilio", conceptType: null, mentions: 1, documents: 1 },
    ],
    edges: [
      { versionId: "ver_1", conceptId: "con_a", mentions: 4, confidence: 0.9 },
      { versionId: "ver_1", conceptId: "con_b", mentions: 1, confidence: 0.7 },
    ],
    truncated: { documents: false, edges: false },
    ...over,
  };
}

function wrap({ children }: { children: ReactNode }) {
  return <BackendProvider>{children}</BackendProvider>;
}

beforeEach(() => {
  // `restoreMocks` restores spies; these are plain `vi.fn()`s and keep their
  // call log across tests in the same file otherwise.
  vi.clearAllMocks();
  backendInfo.mockResolvedValue({ mode: "local", baseUrl: "", tenantId: "", email: "" });
  libraryGraph.mockResolvedValue(envelope());
});

afterEach(() => {
  // A module, so it outlives every tree in this file.
  clearGraphCache();
});

async function ready(opts: { libraryId?: string; floor?: number; active?: boolean } = {}) {
  const view = renderHook(
    (props: { libraryId: string; floor: number; active: boolean }) =>
      useLibraryGraph(props),
    {
      wrapper: wrap,
      initialProps: {
        libraryId: opts.libraryId ?? "lib_a",
        floor: opts.floor ?? 0.6,
        active: opts.active ?? true,
      },
    },
  );
  await waitFor(() => expect(view.result.current.index).not.toBeNull());
  return view;
}

it("asks the server once per floor, and never for a threshold", async () => {
  const view = await ready();
  expect(libraryGraph).toHaveBeenCalledTimes(1);
  // The widest envelope, always. The threshold is not a parameter of this hook
  // at all, which is the strongest form the property can take.
  expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.6, 1);
  view.unmount();
});

it("asks again when the floor changes, because a degree is counted after it", async () => {
  const view = await ready();
  view.rerender({ libraryId: "lib_a", floor: 0.9, active: true });
  await waitFor(() => expect(libraryGraph).toHaveBeenCalledWith("lib_a", 0.9, 1));
  expect(libraryGraph).toHaveBeenCalledTimes(2);
});

it("keeps a library's envelope across a switch away and back", async () => {
  const view = await ready();
  view.rerender({ libraryId: "lib_b", floor: 0.6, active: true });
  await waitFor(() => expect(libraryGraph).toHaveBeenCalledTimes(2));
  view.rerender({ libraryId: "lib_a", floor: 0.6, active: true });
  // No third call: lib_a is still in memory.
  await waitFor(() => expect(view.result.current.index).not.toBeNull());
  expect(libraryGraph).toHaveBeenCalledTimes(2);
});

it("does not touch the network while the graph is not on show", async () => {
  // All seven screens mount at startup and both graph views with them. Without
  // this the envelope downloads and lays itself out on a tab nobody opened.
  const view = renderHook(
    (props: { libraryId: string; floor: number; active: boolean }) =>
      useLibraryGraph(props),
    {
      wrapper: wrap,
      initialProps: { libraryId: "lib_a", floor: 0.6, active: false },
    },
  );
  expect(libraryGraph).not.toHaveBeenCalled();

  view.rerender({ libraryId: "lib_a", floor: 0.6, active: true });
  await waitFor(() => expect(libraryGraph).toHaveBeenCalledTimes(1));
});

it("holds no more envelopes than it says it does", async () => {
  // Five floors are offered and four are held, so a full sweep of the control
  // evicts the one it started on. Asserting the bound is what stops it becoming
  // a comment nobody enforces.
  const view = await ready({ floor: 0.5 });
  for (const floor of [0.6, 0.7, 0.8, 0.9]) {
    view.rerender({ libraryId: "lib_a", floor, active: true });
    await waitFor(() => expect(view.result.current.index).not.toBeNull());
  }
  expect(libraryGraph).toHaveBeenCalledTimes(5);

  view.rerender({ libraryId: "lib_a", floor: 0.5, active: true });
  await waitFor(() => expect(libraryGraph).toHaveBeenCalledTimes(6));
});

it("keeps the last graph that answered when a new floor fails", async () => {
  const view = await ready();
  libraryGraph.mockRejectedValue({ kind: "control_status", message: "503" });
  view.rerender({ libraryId: "lib_a", floor: 0.9, active: true });

  await waitFor(() => expect(view.result.current.status).toBe("failed"));
  // The state is the failed read's; everything drawable is the read that worked.
  expect(view.result.current.index).not.toBeNull();
  expect(view.result.current.error).not.toBeNull();
});

it("shows nothing from another library when a read fails", async () => {
  // The fallback above is scoped: one shelf's picture under another shelf's
  // error would be worse than an empty canvas.
  const view = await ready();
  libraryGraph.mockRejectedValue({ kind: "control_status", message: "503" });
  view.rerender({ libraryId: "lib_b", floor: 0.6, active: true });

  await waitFor(() => expect(view.result.current.status).toBe("failed"));
  expect(view.result.current.index).toBeNull();
});

it("scopes the cache on the plane, so one organisation cannot read another's", async () => {
  // The identity is in the key unconditionally — deliberately not through
  // `scopedKey`, whose bare-local branch exists to avoid orphaning localStorage
  // entries and has nothing to do with a cache that holds a tenant's library.
  expect(graphKey("local", "lib_a", 0.6)).not.toBe(
    graphKey("cloud|https://api|tnt_x|a@b", "lib_a", 0.6),
  );
  // And a floor that arrives as a float is normalised rather than trusted.
  expect(graphKey("local", "lib_a", 0.1 + 0.2)).toBe(graphKey("local", "lib_a", 0.3));
});
