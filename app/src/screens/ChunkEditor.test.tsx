import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { ChunkOverrideRow, ChunkRow, EditOutcome } from "../lib/api";
import { ChunkEditor } from "./ChunkEditor";

/**
 * Editing a chunk is the one place this product deliberately gives up a
 * byte-exact `char_span`, so what is asserted here is that a person is *told*
 * before they do it, that the three verbs stay distinct on the wire, and that
 * a refusal leaves a panel somebody can retry — the recorded `deciding` latch,
 * on the newest screen that can latch.
 */
const { exploreEditChunk } = vi.hoisted(() => ({ exploreEditChunk: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, exploreEditChunk } };
});

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options ?? {});

const CHUNK: ChunkRow = {
  id: "chk_" + "a".repeat(24),
  kind: "cuerpo",
  ordinal: 5,
  text: "el texto original",
  charStart: 0,
  charEnd: 17,
  page: 2,
};

function outcome(over: Partial<EditOutcome> = {}): EditOutcome {
  return {
    versionId: "ver_x", chunkIndex: 5, reindexed: true,
    claimsChecked: 3, claimsUnverified: 0, usd: 0.000005, ...over,
  };
}

function override(over: Partial<ChunkOverrideRow> = {}): ChunkOverrideRow {
  return { chunkIndex: 5, text: null, disabled: false, editedBy: "", ...over };
}

function mount(o?: ChunkOverrideRow) {
  const onDone = vi.fn();
  const view = render(
    <ChunkEditor chunk={CHUNK} versionId="ver_x" override={o} onDone={onDone} />,
  );
  return { ...view, onDone };
}

beforeEach(async () => {
  exploreEditChunk.mockReset();
  exploreEditChunk.mockResolvedValue(outcome());
  await i18n.changeLanguage("en");
});
afterEach(cleanup);

describe("ChunkEditor", () => {
  it("warns what an edit costs before offering the box", () => {
    mount();
    expect(screen.queryByText(t("edit.spanWarning"))).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: t("edit.rewrite") }));
    expect(screen.getByText(t("edit.spanWarning"))).toBeTruthy();
  });

  it("will not save text identical to what is already there", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: t("edit.rewrite") }));
    const save = screen.getByRole("button", { name: t("edit.save") });
    expect(save.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "otra cosa" } });
    expect(save.hasAttribute("disabled")).toBe(false);
  });

  it("sends the rewritten text and keeps the hidden flag as it was", async () => {
    mount(override({ disabled: true }));
    fireEvent.click(screen.getByRole("button", { name: t("edit.rewrite") }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "corregido" } });
    fireEvent.click(screen.getByRole("button", { name: t("edit.save") }));
    await waitFor(() => expect(exploreEditChunk).toHaveBeenCalled());
    expect(exploreEditChunk).toHaveBeenCalledWith({
      versionId: "ver_x", chunkIndex: 5, text: "corregido", disabled: true,
    });
  });

  it("hiding a chunk changes no text, because they are different verbs", async () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: t("edit.hide") }));
    await waitFor(() => expect(exploreEditChunk).toHaveBeenCalled());
    expect(exploreEditChunk.mock.calls[0]![0]).toEqual({
      versionId: "ver_x", chunkIndex: 5, text: null, disabled: true,
    });
  });

  it("hiding an already-rewritten chunk keeps the rewrite", async () => {
    mount(override({ text: "ya corregido" }));
    fireEvent.click(screen.getByRole("button", { name: t("edit.hide") }));
    await waitFor(() => expect(exploreEditChunk).toHaveBeenCalled());
    expect(exploreEditChunk.mock.calls[0]![0].text).toBe("ya corregido");
  });

  it("undo is a null text and no flag, which deletes the override", async () => {
    mount(override({ text: "corregido", disabled: true }));
    fireEvent.click(screen.getByRole("button", { name: t("edit.undo") }));
    await waitFor(() => expect(exploreEditChunk).toHaveBeenCalled());
    expect(exploreEditChunk.mock.calls[0]![0]).toEqual({
      versionId: "ver_x", chunkIndex: 5, text: null, disabled: false,
    });
  });

  it("offers no undo for a chunk nobody has touched", () => {
    mount();
    expect(screen.queryByRole("button", { name: t("edit.undo") })).toBeNull();
  });

  it("says what the edit cost, and what it cost the claims", async () => {
    exploreEditChunk.mockResolvedValue(outcome({ claimsUnverified: 2 }));
    mount();
    fireEvent.click(screen.getByRole("button", { name: t("edit.hide") }));
    await waitFor(() =>
      expect(screen.getByText(/0\.000005/, { exact: false })).toBeTruthy(),
    );
    expect(
      screen.getByText(t("edit.claimsLostTheirSpan", { n: 2 }), { exact: false }),
    ).toBeTruthy();
  });

  it("a refusal shows the guidance and leaves the buttons answerable", async () => {
    exploreEditChunk.mockRejectedValue({
      kind: "control_status", controlKind: "graph_unreachable",
      message: "503: bolt",
    });
    const { onDone } = mount();
    fireEvent.click(screen.getByRole("button", { name: t("edit.hide") }));
    await waitFor(() =>
      expect(screen.getByText(t("error.graphUnreachable"))).toBeTruthy(),
    );
    expect(
      screen.getByRole("button", { name: t("edit.hide") }).hasAttribute("disabled"),
    ).toBe(false);
    expect(onDone).not.toHaveBeenCalled();
  });
});
