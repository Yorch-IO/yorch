/**
 * The error contract, which had no test at all.
 *
 * `detail.kind` is what turns a status code into advice a person can act on,
 * and it is the one thing both control planes agree to emit — the NestJS plane
 * reproduces FastAPI's body shape specifically so this code does not have to
 * know which one answered. Everything here is a decision about a string, so it
 * is asserted on the functions rather than by rendering a screen.
 */
import { describe, expect, it } from "vitest";

import { controlErrorKind, errorGuidanceKey, errorMessage, isAppError } from "./api";

const control = (body: string, over: Record<string, unknown> = {}) => ({
  kind: "control_status" as const,
  message: `the control API returned 403: ${body}`,
  ...over,
});

describe("reading the control API's own kind", () => {
  it("takes the tag Rust read off the untruncated body", () => {
    // The path that always works: Rust holds the whole body, the webview holds
    // a message cut to 500 characters.
    expect(
      controlErrorKind(control("…", { controlKind: "no_membership" })),
    ).toBe("no_membership");
  });

  it("still reads it out of the message for a shell that does not send one", () => {
    const body = JSON.stringify({ detail: { kind: "user_inactive", message: "…" } });
    expect(controlErrorKind(control(body))).toBe("user_inactive");
  });

  it("survives a body cut mid-object, with no tag rather than a throw", () => {
    // What used to happen to every long body, silently. It now only happens
    // when Rust could not read a tag either, and an error screen must render
    // regardless.
    const cut = '{"detail": {"kind": "tenant_required", "tenants": ["tnt_aaaa';
    expect(controlErrorKind(control(cut))).toBeUndefined();
    expect(errorGuidanceKey(control(cut))).toBeUndefined();
  });

  it("has no tag for an unmatched route, which answers FastAPI's bare shape", () => {
    // `{"detail": "Not Found"}` — a string, not an object. A kind the guidance
    // map has never heard of would be worse than no kind.
    expect(controlErrorKind(control('{"detail": "Not Found"}'))).toBeUndefined();
  });

  it("has no tag for a failure that never reached the control API", () => {
    expect(controlErrorKind({ kind: "control_unreachable", message: "…" })).toBeUndefined();
    expect(controlErrorKind(new Error("boom"))).toBeUndefined();
    expect(controlErrorKind("boom")).toBeUndefined();
  });
});

describe("the advice a failure carries", () => {
  it.each([
    ["unauthenticated", "error.unauthenticated"],
    ["unknown_user", "error.unknownUser"],
    ["user_inactive", "error.userInactive"],
    ["no_membership", "error.noMembership"],
    ["tenant_required", "error.tenantRequired"],
    ["tenant_scope_pending", "error.tenantScopePending"],
  ])("maps %s, which only the paid plane can return", (kind, key) => {
    // Every authentication and tenancy failure has a different fix, and none of
    // them is stated by the message alone.
    expect(errorGuidanceKey(control("…", { controlKind: kind }))).toBe(key);
  });

  it("prefers the control API's advice over Rust's own", () => {
    // Both can be present: a `control_status` is a Rust kind carrying a control
    // kind, and the inner one is the specific of the two.
    expect(errorGuidanceKey(control("…", { controlKind: "run_not_found" }))).toBe(
      "error.runNotFound",
    );
  });

  it("falls back to Rust's kind when the body carried none", () => {
    expect(errorGuidanceKey({ kind: "not_signed_in", message: "…" })).toBe(
      "error.notSignedIn",
    );
  });

  it("offers nothing rather than something generic for a kind with no advice", () => {
    // `docker_missing` and `io` say everything useful in their message; a
    // guidance line repeating it would train the reader to skip both.
    expect(errorGuidanceKey({ kind: "io", message: "…" })).toBeUndefined();
    expect(errorGuidanceKey(control("…", { controlKind: "gate_not_ready" }))).toBeUndefined();
  });
});

describe("what a failure says when it is shown", () => {
  it("recognises the shape Rust serialises", () => {
    expect(isAppError({ kind: "io", message: "x" })).toBe(true);
    expect(isAppError({ kind: "io" })).toBe(false);
    expect(isAppError(null)).toBe(false);
  });

  it("renders something for anything that can be thrown", () => {
    expect(errorMessage({ kind: "io", message: "no such file" })).toBe("no such file");
    expect(errorMessage(new Error("boom"))).toBe("boom");
    expect(errorMessage("boom")).toBe("boom");
  });
});
