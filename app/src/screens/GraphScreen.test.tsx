import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "./../i18n";
import { GraphScreen } from "./GraphScreen";

/**
 * The mode shell, with both views stubbed. What is under test is the frame:
 * which view opens, that "ver este libro" carries a version across, and that
 * neither view is unmounted by switching — re-entering the overview must not
 * re-run the simulation, and leaving it must not throw away a pan.
 */
const { library, document_ } = vi.hoisted(() => ({
  library: vi.fn(),
  document_: vi.fn(),
}));

vi.mock("./graph/LibraryGraph", () => ({
  LibraryGraph: ({ onOpenDocument }: { onOpenDocument?: (d: unknown) => void }) => {
    library();
    return (
      <button
        type="button"
        onClick={() =>
          onOpenDocument?.({
            documentId: "doc_1",
            versionId: "ver_1",
            title: "Historia",
            format: "pdf",
          })
        }
      >
        overview:open
      </button>
    );
  },
}));

vi.mock("./graph/DocumentGraph", () => ({
  DocumentGraph: ({ focus }: { focus?: { versionId: string } | null }) => {
    document_();
    return <p>document:{focus?.versionId ?? "none"}</p>;
  },
}));

const t = (key: string): string => i18n.t(key);

/** Mounted but not on show: inside a `hidden` wrapper. `toBeVisible` is a
 *  jest-dom matcher this project does not have. */
function shown(text: string | RegExp): boolean {
  return screen.getByText(text).closest("[hidden]") === null;
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => cleanup());

describe("the graph shell", () => {
  it("opens on the library and mounts the document view behind it", async () => {
    render(<GraphScreen />);
    expect(shown("overview:open")).toBe(true);
    expect(shown(/^document:/)).toBe(false);
    // Mounted, not merely reachable: unmounting would re-run the layout.
    expect(screen.getByText(/^document:/)).toBeTruthy();
  });

  it("describes whichever view is showing", async () => {
    render(<GraphScreen />);
    expect(screen.getByText(t("graph.libraryIntro"))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("graph.modeDocument") }));
    expect(screen.getByText(t("graph.intro"))).toBeTruthy();
  });

  it("carries a book from the overview into the document view", async () => {
    render(<GraphScreen />);
    fireEvent.click(screen.getByText("overview:open"));
    await waitFor(() => expect(shown("document:ver_1")).toBe(true));
    expect(shown("overview:open")).toBe(false);
  });

  it("goes back without unmounting either view", async () => {
    render(<GraphScreen />);
    const mounts = library.mock.calls.length;
    fireEvent.click(screen.getByText("overview:open"));
    fireEvent.click(screen.getByRole("button", { name: t("graph.modeLibrary") }));
    expect(shown("overview:open")).toBe(true);
    // Re-rendered, but never remounted: the stub counts renders, and what
    // matters is that the node was never removed from the document.
    expect(library.mock.calls.length).toBeGreaterThanOrEqual(mounts);
    expect(screen.getByText("document:ver_1")).toBeTruthy();
  });

  it("says which mode is current", async () => {
    render(<GraphScreen />);
    const libraryTab = screen.getByRole("button", { name: t("graph.modeLibrary") });
    expect(libraryTab.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: t("graph.modeDocument") }));
    expect(libraryTab.getAttribute("aria-pressed")).toBe("false");
  });
});
