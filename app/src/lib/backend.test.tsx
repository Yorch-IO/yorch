import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendInfo, LibraryRow } from "./api";
import { BackendProvider, backendIdentity, scopedKey, useBackend } from "./backend";
import { LibrariesProvider, useLibraries } from "./libraries";

/**
 * Switching planes, which used to change where requests went and nothing a
 * person could see.
 *
 * The picker fetched its rows once, at launch, so after a switch the shelf, the
 * graph and the question all still described the other plane — and because a
 * library id is chosen by the client (`lib_teologia` is the one two
 * organisations pick independently), a remembered id could *validate* against
 * the new list and show a name for a corpus that was no longer there. That last
 * case is the one worth a test: nothing about it looks wrong on screen.
 */
const { libraries, backendSettings } = vi.hoisted(() => ({
  libraries: vi.fn(),
  backendSettings: vi.fn(),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, libraries, backendSettings } };
});

const info = (over: Partial<BackendInfo> = {}): BackendInfo => ({
  mode: "local",
  baseUrl: "",
  tenantId: "",
  signedIn: false,
  email: "",
  secretStore: "keychain",
  ...over,
});

const row = (id: string, name = ""): LibraryRow => ({
  id,
  name,
  language: "es",
  documents: 1,
  indexedVersions: 1,
});

const CLOUD = info({
  mode: "cloud",
  baseUrl: "https://api.example",
  signedIn: true,
  email: "alguien@example.com",
});

/** Reads both contexts and offers a way to publish a plane, which is what
 *  `StackScreen` does after a save, a sign-in or a sign-out. */
function Probe() {
  const { identity, publish } = useBackend();
  const { rows, selected, select } = useLibraries();
  return (
    <div>
      <p data-testid="identity">{identity}</p>
      <p data-testid="selected">{selected}</p>
      <p data-testid="rows">{rows.map((r) => r.id).join(",")}</p>
      <button type="button" onClick={() => select("lib_teologia")}>
        pick
      </button>
      <button type="button" onClick={() => publish(CLOUD)}>
        cloud
      </button>
      <button type="button" onClick={() => publish(info())}>
        local
      </button>
    </div>
  );
}

function mount() {
  return render(
    <BackendProvider>
      <LibrariesProvider>
        <Probe />
      </LibrariesProvider>
    </BackendProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  backendSettings.mockResolvedValue(info());
  libraries.mockResolvedValue({ libraries: [row("lib_teologia", "Teología")] });
});

// Vitest exposes no global afterEach hook here, so RTL never registers its own
// cleanup and each file calls it by hand.
afterEach(cleanup);

describe("the backend identity", () => {
  it("is one value for the local plane, which is one organisation by construction", () => {
    expect(backendIdentity(info())).toBe("local");
    expect(backendIdentity(info({ baseUrl: "https://ignored" }))).toBe("local");
  });

  it("separates two addresses, two organisations and two people", () => {
    const a = backendIdentity(CLOUD);
    expect(backendIdentity({ ...CLOUD, baseUrl: "https://otra" })).not.toBe(a);
    expect(backendIdentity({ ...CLOUD, tenantId: "acme" })).not.toBe(a);
    expect(backendIdentity({ ...CLOUD, email: "otro@example.com" })).not.toBe(a);
  });

  it("makes being signed out its own identity, because nothing can be read there", () => {
    expect(backendIdentity({ ...CLOUD, signedIn: false, email: "" })).not.toBe(
      backendIdentity(CLOUD),
    );
  });

  it("leaves the local plane's stored keys exactly where they were", () => {
    // The same decision as `_salt()` returning nothing for the legacy tenant:
    // scoping unconditionally would orphan every history and every remembered
    // library that exists today.
    expect(scopedKey("companyBrain.libraryId", "local")).toBe("companyBrain.libraryId");
    expect(scopedKey("companyBrain.libraryId", "cloud|x||y")).toBe(
      "companyBrain.libraryId.cloud|x||y",
    );
  });
});

describe("switching planes", () => {
  it("fetches the libraries again, which is the whole defect", async () => {
    mount();
    await waitFor(() => expect(libraries).toHaveBeenCalledTimes(1));

    libraries.mockResolvedValue({ libraries: [row("lib_acme", "Acme")] });
    fireEvent.click(screen.getByText("cloud"));

    await waitFor(() => expect(screen.getByTestId("rows").textContent).toBe("lib_acme"));
    expect(screen.getByTestId("selected").textContent).toBe("lib_acme");
  });

  it("does not carry a selection across on the strength of a shared id", async () => {
    mount();
    await waitFor(() => expect(libraries).toHaveBeenCalled());
    fireEvent.click(screen.getByText("pick"));
    expect(screen.getByTestId("selected").textContent).toBe("lib_teologia");

    // The paid plane happens to hold a library with the same id and a different
    // corpus. Keeping the selection would be silent and wrong; what must decide
    // is what *this* plane remembered, and it has remembered nothing.
    libraries.mockResolvedValue({
      libraries: [row("lib_otros", "Otros"), row("lib_teologia", "Teología de Acme")],
    });
    fireEvent.click(screen.getByText("cloud"));

    await waitFor(() =>
      expect(screen.getByTestId("rows").textContent).toBe("lib_otros,lib_teologia"),
    );
    expect(screen.getByTestId("selected").textContent).toBe("lib_otros");
  });

  it("remembers each plane's own choice and restores it on the way back", async () => {
    mount();
    await waitFor(() => expect(libraries).toHaveBeenCalled());
    fireEvent.click(screen.getByText("pick"));

    libraries.mockResolvedValue({
      libraries: [row("lib_otros"), row("lib_teologia")],
    });
    fireEvent.click(screen.getByText("cloud"));
    await waitFor(() => expect(screen.getByTestId("selected").textContent).toBe("lib_otros"));
    fireEvent.click(screen.getByText("pick"));
    await waitFor(() => expect(screen.getByTestId("selected").textContent).toBe("lib_teologia"));

    libraries.mockResolvedValue({ libraries: [row("lib_teologia", "Teología")] });
    fireEvent.click(screen.getByText("local"));
    await waitFor(() => expect(screen.getByTestId("identity").textContent).toBe("local"));
    expect(screen.getByTestId("selected").textContent).toBe("lib_teologia");

    // Two keys, and the local plane's is the bare one.
    expect(window.localStorage.getItem("companyBrain.libraryId")).toBe("lib_teologia");
    expect(
      window.localStorage.getItem(scopedKey("companyBrain.libraryId", backendIdentity(CLOUD))),
    ).toBe("lib_teologia");
  });

  it("falls back to the local plane when Rust cannot say which one is chosen", async () => {
    // Not an unknown third scope: guessing anything else consults a stored key
    // nothing has ever written and reports an empty shelf as the first thing a
    // user sees.
    backendSettings.mockRejectedValue(new Error("sin rust"));
    mount();
    await waitFor(() => expect(screen.getByTestId("identity").textContent).toBe("local"));
  });
});
