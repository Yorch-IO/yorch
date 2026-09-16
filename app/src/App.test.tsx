import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import i18n from "./i18n";

/**
 * The shell, with its seven screens stubbed out. Stubbing them is the point: what
 * is being checked here is the frame — that one picker serves every screen,
 * that the screens stay mounted, and that only one of them is visible — and
 * mounting the real ones would drag in five screens' worth of API calls to
 * prove none of it.
 */
const { libraries } = vi.hoisted(() => ({ libraries: vi.fn() }));

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return { ...actual, api: { ...actual.api, libraries } };
});

vi.mock("./screens/HomeScreen", () => ({ HomeScreen: () => <p>screen:home</p> }));
vi.mock("./screens/StackScreen", () => ({ StackScreen: () => <p>screen:stack</p> }));
vi.mock("./screens/LibraryScreen", () => ({ LibraryScreen: () => <p>screen:library</p> }));
vi.mock("./screens/ExploreScreen", () => ({ ExploreScreen: () => <p>screen:explore</p> }));
vi.mock("./screens/GraphScreen", () => ({ GraphScreen: () => <p>screen:graph</p> }));
vi.mock("./screens/ImportScreen", () => ({ ImportScreen: () => <p>screen:import</p> }));
vi.mock("./screens/AskScreen", () => ({ AskScreen: () => <p>screen:ask</p> }));
vi.mock("./screens/ChannelScreen", () => ({ ChannelScreen: () => <p>screen:channel</p> }));

const t = (key: string): string => i18n.t(key);

/** A screen is on the shelf but not on show: mounted, inside a `hidden`
 *  wrapper. `toBeVisible` is a jest-dom matcher this project does not have, so
 *  the wrapper is asked directly. */
function shown(name: string): boolean {
  const node = screen.getByText(`screen:${name}`);
  return node.closest("[hidden]") === null;
}

beforeEach(() => {
  libraries.mockResolvedValue({
    libraries: [
      { id: "lib_a", name: "Biblioteca A", language: "es", documents: 2, indexedVersions: 2 },
    ],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the shell", () => {
  it("mounts every screen and shows one", async () => {
    // Not a detail: unmounting a screen threw away its state, and stopped
    // ImportScreen's gate poller mid-run. The `hidden` attribute is what keeps
    // them alive, and a stray `display` rule in the CSS would defeat it.
    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());

    for (const name of [
      "home",
      "stack",
      "library",
      "explore",
      "graph",
      "import",
      "channel",
      "ask",
    ]) {
      expect(screen.getByText(`screen:${name}`), name).toBeTruthy();
    }
    // Home, not Services: "what is in here?" is the question a person arrives
    // with, and Services is a diagnostic panel.
    expect(shown("home")).toBe(true);
    expect(shown("stack")).toBe(false);
    expect(shown("ask")).toBe(false);
  });

  it("switches which screen is shown without unmounting the others", async () => {
    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: t("nav.ask") }));

    expect(shown("ask")).toBe(true);
    expect(shown("home")).toBe(false);
    expect(screen.getByText("screen:home")).toBeTruthy();
  });

  it("renders one library picker for the whole app", async () => {
    // It used to be rendered by each of the five screens that act on a
    // library, over this same shared state. All five are mounted at once, so
    // that is five copies of one control in one document.
    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: t("nav.ask") }));

    expect(await screen.findAllByLabelText(t("libraries.label"))).toHaveLength(1);
  });

  it("leaves the picker off the screens that own no library", async () => {
    // Home's figures are project-wide, so a picker there would offer a choice
    // that changes nothing on the screen; Services is what you use before there
    // is anything to pick; and Channel picks a *channel*, which is what its
    // library is derived from — two pickers over one choice could disagree.
    render(<App />);
    await waitFor(() => expect(libraries).toHaveBeenCalled());

    expect(screen.queryByLabelText(t("libraries.label"))).toBeNull();
    // …and the bar itself stays, so arriving here shifts nothing.
    expect(screen.getByLabelText(t("nav.language"))).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: t("nav.stack") }));
    expect(screen.queryByLabelText(t("libraries.label"))).toBeNull();
    expect(screen.getByLabelText(t("nav.language"))).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: t("nav.channel") }));
    expect(screen.queryByLabelText(t("libraries.label"))).toBeNull();

    // …and it comes back on a screen that does own one.
    fireEvent.click(screen.getByRole("button", { name: t("nav.ask") }));
    expect(await screen.findByLabelText(t("libraries.label"))).toBeTruthy();
  });
});
