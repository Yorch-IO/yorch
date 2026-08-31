import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { BackendInfo } from "../lib/api";
import { BackendProvider } from "../lib/backend";
import { StackScreen } from "./StackScreen";

/**
 * The backend switch: the only place in the app that knows there is more than
 * one control plane.
 *
 * What is worth asserting here is the *shape of the choice*, not the plumbing —
 * the headers, the persistence and the address validation are Rust's, and are
 * tested there against a real socket. This covers what a person can do wrong:
 * pick the paid service without an address, or expect the local one to ask for
 * a sign-in it does not have.
 */
const {
  backendSettings,
  setBackendMode,
  signIn,
  signOut,
  dockerProbe,
  stackStatus,
  providerSettings,
  controlHealth,
} = vi.hoisted(() => ({
    backendSettings: vi.fn(),
    setBackendMode: vi.fn(),
    signIn: vi.fn(),
    signOut: vi.fn(),
    dockerProbe: vi.fn(),
    stackStatus: vi.fn(),
    providerSettings: vi.fn(),
    controlHealth: vi.fn(),
  }));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      backendSettings,
      setBackendMode,
      signIn,
      signOut,
      dockerProbe,
      stackStatus,
      providerSettings,
      controlHealth,
    },
  };
});

const t = (key: string): string => i18n.t(key);

const info = (over: Partial<BackendInfo> = {}): BackendInfo => ({
  mode: "local",
  baseUrl: "",
  tenantId: "",
  signedIn: false,
  email: "",
  secretStore: "keychain",
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  backendSettings.mockResolvedValue(info());
  setBackendMode.mockImplementation((mode, baseUrl, tenantId) =>
    Promise.resolve(info({ mode, baseUrl, tenantId })),
  );
  signIn.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x", signedIn: true, email: "alguien@example.com" }));
  signOut.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x" }));
  dockerProbe.mockRejectedValue(new Error("sin docker"));
  stackStatus.mockRejectedValue(new Error("sin pila"));
  providerSettings.mockRejectedValue(new Error("sin proveedor"));
  controlHealth.mockRejectedValue(new Error("sin api"));
});

// Vitest exposes no global afterEach hook here, so RTL never registers its own
// cleanup and each file calls it by hand.
afterEach(cleanup);

describe("choosing a backend", () => {
  it("opens on the local plane, which needs no address and no sign-in", async () => {
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    await waitFor(() => expect(backendSettings).toHaveBeenCalled());

    const select = await screen.findByLabelText(t("backend.mode"));
    expect((select as HTMLSelectElement).value).toBe("local");
    expect(screen.getByText(t("backend.localNote"))).toBeTruthy();
    // No address field: the local plane's port is discovered, not typed.
    expect(screen.queryByLabelText(t("backend.baseUrl"))).toBeNull();
  });

  it("asks for an address only for the paid plane", async () => {
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    const select = await screen.findByLabelText(t("backend.mode"));
    fireEvent.change(select, { target: { value: "cloud" } });

    expect(screen.getByLabelText(t("backend.baseUrl"))).toBeTruthy();
    // And drops the note that exists to say the local plane needs no account.
    expect(screen.queryByText(t("backend.localNote"))).toBeNull();
  });

  it("asks for no organisation, because this release supports exactly one", async () => {
    // The server resolves a sole membership from the token, so a field here
    // would be a choice with one option and a way to get it wrong. The header
    // itself stays in Rust — three tests in `control.rs` pin it — because that
    // is what a picker would use the day several memberships exist.
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    fireEvent.change(await screen.findByLabelText(t("backend.mode")), {
      target: { value: "cloud" },
    });
    // Named rather than counted: `providerSettings` is rejected in this suite so
    // its field never renders, and a bare count would start failing for a
    // reason that has nothing to do with organisations.
    expect(screen.getAllByRole("textbox")).toEqual([
      screen.getByLabelText(t("backend.baseUrl")),
    ]);
  });

  it("passes the whole choice to Rust, which is what validates it", async () => {
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    const select = await screen.findByLabelText(t("backend.mode"));
    fireEvent.change(select, { target: { value: "cloud" } });
    fireEvent.change(screen.getByLabelText(t("backend.baseUrl")), {
      target: { value: "https://brain.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("backend.save") }));

    await waitFor(() =>
      // The empty third argument is an *absent* `X-Tenant-Id`, which is what
      // tells the server to use the sole membership — not an empty header.
      expect(setBackendMode).toHaveBeenCalledWith("cloud", "https://brain.example.com", ""),
    );
  });

  it("still reads the control API health in cloud mode", async () => {
    // The regression that got past everything: `stack_status` refuses in cloud
    // mode — correctly — and `refresh()` treated that refusal as fatal and
    // returned, so the health read underneath it never ran. The paid plane was
    // never contacted from a signed-in window, which showed up not as a failure
    // but as a `last_login_at` that never moved.
    backendSettings.mockResolvedValue(
      info({ mode: "cloud", baseUrl: "https://b.example.com", signedIn: true }),
    );
    stackStatus.mockRejectedValue({ kind: "config", message: "es del backend local" });
    controlHealth.mockResolvedValue({ ok: true, services: {} });

    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    await waitFor(() => expect(controlHealth).toHaveBeenCalled());
    // And the local stack is not asked about at all, rather than asked and
    // forgiven: a command that refuses is one that should not have been called.
    expect(stackStatus).not.toHaveBeenCalled();
    expect(providerSettings).not.toHaveBeenCalled();
  });

  it("does not report a missing local stack as an error in cloud mode", async () => {
    // Docker is a requirement of the local backend and of nothing else. The
    // screen already holds that rule for `dockerProbe`; it has to hold one call
    // earlier too, or the paid user opens on a red panel about a subsystem they
    // do not use.
    backendSettings.mockResolvedValue(
      info({ mode: "cloud", baseUrl: "https://b.example.com", signedIn: true }),
    );
    controlHealth.mockResolvedValue({ ok: true, services: {} });
    const { container } = render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    await waitFor(() => expect(controlHealth).toHaveBeenCalled());
    expect(container.querySelector(".error")).toBeNull();
  });

  it("still describes the local stack in local mode", async () => {
    // The other half: gating on the mode must not stop the local backend from
    // being described, which is this screen's original job.
    stackStatus.mockResolvedValue({
      project: "company-brain", composeDir: "/x", workspace: "/w",
      ports: { qdrantHttp: 6433, qdrantGrpc: 6434, postgres: 5532, temporal: 7333, temporalUi: 8380, api: 8787 },
      devMode: true, services: [],
    });
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    await waitFor(() => expect(stackStatus).toHaveBeenCalled());
    expect(providerSettings).toHaveBeenCalled();
  });

  it("says where the session is kept, which is not the same everywhere", async () => {
    // A Linux box with no Secret Service falls back to an owner-only file. That
    // is a real difference in how well a thirty-day credential is protected,
    // and Rust has reported it since the keychain landed while nothing read it.
    backendSettings.mockResolvedValue(
      info({ mode: "cloud", baseUrl: "https://b.example.com", signedIn: true, secretStore: "file" }),
    );
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    expect(await screen.findByText(t("backend.secretStore.file"))).toBeTruthy();
    expect(screen.queryByText(t("backend.secretStore.keychain"))).toBeNull();
  });

  it("says plainly when the paid plane has no session", async () => {
    // Every request would be refused, and a screen that showed nothing would
    // leave the user reading a 401 on some other tab.
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x", signedIn: false }));
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    expect(await screen.findByText(t("backend.signedOut"))).toBeTruthy();
  });

  it("stays reachable on a machine with no Docker", async () => {
    // The case the paid service exists for. Docker's absence used to replace
    // the whole screen with an install prompt, so the one user for whom Docker
    // is irrelevant had no way to switch away from the backend that needs it.
    dockerProbe.mockRejectedValue(new Error("Docker no está instalado"));
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    expect(await screen.findByLabelText(t("backend.mode"))).toBeTruthy();
  });

  it("does not treat a missing Docker as an error once the paid plane is chosen", async () => {
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x", signedIn: true }));
    dockerProbe.mockRejectedValue(new Error("Docker no está instalado"));
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    await screen.findByLabelText(t("backend.mode"));
    // Not an error: Docker is a requirement of the local backend and nothing else.
    expect(screen.queryByText(t("docker.missingTitle"))).toBeNull();
  });

  it("offers a sign-in when the paid plane has no session, and names who signed in", async () => {
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x" }));
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: t("backend.signIn") }));
    await waitFor(() => expect(signIn).toHaveBeenCalled());
    // The email is a label, read from the token's claim. It authorizes nothing.
    expect(
      await screen.findByText(
        i18n.t("backend.signedInAs", { email: "alguien@example.com" }),
      ),
    ).toBeTruthy();
  });

  it("keeps the sign-in button pressed once while the browser has the user", async () => {
    // Minutes, realistically: a password and possibly a one-time code. A second
    // press would bind a second listener to the same port and fail with an
    // error about an address in use, which says nothing about what happened.
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x" }));
    let release: (v: unknown) => void = () => {};
    signIn.mockImplementation(() => new Promise((r) => (release = r)));
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    const button = await screen.findByRole("button", { name: t("backend.signIn") });
    fireEvent.click(button);
    // Plain DOM assertions: this project does not install jest-dom, so
    // `toBeDisabled` is not a matcher here.
    await waitFor(() => {
      const waiting = screen.getByRole("button", {
        name: t("backend.signingIn"),
      }) as HTMLButtonElement;
      expect(waiting.disabled).toBe(true);
    });
    release(info({ mode: "cloud", baseUrl: "https://x", signedIn: true }));
  });

  it("says that signing out is local, because it is", async () => {
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x", signedIn: true }));
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    expect(await screen.findByText(t("backend.signOutNote"))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("backend.signOut") }));
    await waitFor(() => expect(signOut).toHaveBeenCalled());
  });

  it("does not nag about a session in local mode, which has no accounts", async () => {
    render(
      <BackendProvider>
        <StackScreen />
      </BackendProvider>,
    );
    await waitFor(() => expect(backendSettings).toHaveBeenCalled());
    expect(screen.queryByText(t("backend.signedOut"))).toBeNull();
    expect(screen.queryByText(t("backend.signedIn"))).toBeNull();
  });
});
