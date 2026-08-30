import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import type { BackendInfo } from "../lib/api";
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
    render(<StackScreen />);
    await waitFor(() => expect(backendSettings).toHaveBeenCalled());

    const select = await screen.findByLabelText(t("backend.mode"));
    expect((select as HTMLSelectElement).value).toBe("local");
    expect(screen.getByText(t("backend.localNote"))).toBeTruthy();
    // No address field: the local plane's port is discovered, not typed.
    expect(screen.queryByLabelText(t("backend.baseUrl"))).toBeNull();
  });

  it("asks for an address and an organisation only for the paid plane", async () => {
    render(<StackScreen />);
    const select = await screen.findByLabelText(t("backend.mode"));
    fireEvent.change(select, { target: { value: "cloud" } });

    expect(screen.getByLabelText(t("backend.baseUrl"))).toBeTruthy();
    // Optional, and the hint has to say so: an account in one organisation
    // sends no header and lets the server use its sole membership.
    expect(screen.getByPlaceholderText(t("backend.tenantHint"))).toBeTruthy();
    expect(screen.queryByText(t("backend.localNote"))).toBeNull();
  });

  it("passes the whole choice to Rust, which is what validates it", async () => {
    render(<StackScreen />);
    const select = await screen.findByLabelText(t("backend.mode"));
    fireEvent.change(select, { target: { value: "cloud" } });
    fireEvent.change(screen.getByLabelText(t("backend.baseUrl")), {
      target: { value: "https://brain.example.com" },
    });
    fireEvent.change(screen.getByLabelText(t("backend.tenant")), {
      target: { value: "tnt_x" },
    });
    fireEvent.click(screen.getByRole("button", { name: t("backend.save") }));

    await waitFor(() =>
      expect(setBackendMode).toHaveBeenCalledWith(
        "cloud",
        "https://brain.example.com",
        "tnt_x",
      ),
    );
  });

  it("says plainly when the paid plane has no session", async () => {
    // Every request would be refused, and a screen that showed nothing would
    // leave the user reading a 401 on some other tab.
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x", signedIn: false }));
    render(<StackScreen />);
    expect(await screen.findByText(t("backend.signedOut"))).toBeTruthy();
  });

  it("stays reachable on a machine with no Docker", async () => {
    // The case the paid service exists for. Docker's absence used to replace
    // the whole screen with an install prompt, so the one user for whom Docker
    // is irrelevant had no way to switch away from the backend that needs it.
    dockerProbe.mockRejectedValue(new Error("Docker no está instalado"));
    render(<StackScreen />);
    expect(await screen.findByLabelText(t("backend.mode"))).toBeTruthy();
  });

  it("does not treat a missing Docker as an error once the paid plane is chosen", async () => {
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x", signedIn: true }));
    dockerProbe.mockRejectedValue(new Error("Docker no está instalado"));
    render(<StackScreen />);
    await screen.findByLabelText(t("backend.mode"));
    // Not an error: Docker is a requirement of the local backend and nothing else.
    expect(screen.queryByText(t("docker.missingTitle"))).toBeNull();
  });

  it("offers a sign-in when the paid plane has no session, and names who signed in", async () => {
    backendSettings.mockResolvedValue(info({ mode: "cloud", baseUrl: "https://x" }));
    render(<StackScreen />);
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
    render(<StackScreen />);
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
    render(<StackScreen />);
    expect(await screen.findByText(t("backend.signOutNote"))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("backend.signOut") }));
    await waitFor(() => expect(signOut).toHaveBeenCalled());
  });

  it("does not nag about a session in local mode, which has no accounts", async () => {
    render(<StackScreen />);
    await waitFor(() => expect(backendSettings).toHaveBeenCalled());
    expect(screen.queryByText(t("backend.signedOut"))).toBeNull();
    expect(screen.queryByText(t("backend.signedIn"))).toBeNull();
  });
});
