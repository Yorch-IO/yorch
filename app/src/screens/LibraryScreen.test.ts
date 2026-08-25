import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The one guarantee the Library screen owes: a permanent removal cannot be
 * triggered by a single click.
 *
 * **This reads the source rather than rendering the component**, and that is a
 * deliberate weaker substitute, not an oversight. A rendering test would be
 * stronger — it would prove the button wiring rather than infer it — but it
 * needs `@testing-library/react` and `jsdom`, and this project has no component
 * test stack at all. Scanning source for a structural property is a pattern
 * already established here by `i18n.test.ts`, which finds unused keys the same
 * way. What this catches is the realistic regression: a refactor wiring
 * "Eliminar" straight to the removal call. What it cannot catch is a confirm
 * panel that renders but whose buttons do nothing.
 */
const SOURCE = readFileSync(
  join(process.cwd(), "src", "screens", "LibraryScreen.tsx"),
  "utf8",
);

describe("removal is confirmed before it happens", () => {
  it("calls the removal API from exactly one place", () => {
    const calls = SOURCE.match(/api\.documentRemove\(/g) ?? [];
    expect(calls).toHaveLength(1);
  });

  it("puts the confirmation between the button and the call", () => {
    // The action button only arms the confirmation…
    expect(SOURCE).toMatch(/onClick=\{\(\) => setConfirming\(d\.id\)\}/);
    // …and the call sits behind the confirm branch, which is guarded on
    // `confirming === d.id`.
    expect(SOURCE).toMatch(/confirming === d\.id/);
    expect(SOURCE).toMatch(/className="confirm"/);
  });

  it("never wires the remove action directly to the removal call", () => {
    expect(SOURCE).not.toMatch(/onClick=\{\(\) => void remove\(d\.id\)\}[\s\S]{0,400}?library\.actions\.remove/);
  });

  it("states what survives a removal, not only what is destroyed", () => {
    // An irreversible act reported only as "done" leaves the user to guess
    // whether their cost history went with it.
    expect(SOURCE).toContain("library.confirm.kept");
    expect(SOURCE).toContain("library.removedKept");
  });

  it("does not reach for the dialog plugin", () => {
    // Capabilities are deny-by-default and the grant is deliberately minimal.
    // `dialog:allow-confirm` is not granted, so this would fail at runtime with
    // nothing visible to explain it.
    expect(SOURCE).not.toContain("@tauri-apps/plugin-dialog");
    expect(SOURCE).not.toMatch(/\bwindow\.confirm\(/);
  });
});

describe("the three verbs are told apart by what they cost", () => {
  it("disables reindex and rebuild on the server's own flags", () => {
    // Computed server-side, because both are catalog questions: a missing
    // source path and a pruned run directory. Inferring them here would put the
    // rule in two places.
    expect(SOURCE).toContain("!detail.canReindex");
    expect(SOURCE).toContain("!detail.canRebuild");
  });

  it("explains a disabled verb instead of leaving a dead button", () => {
    expect(SOURCE).toContain("library.cannotReindex");
    expect(SOURCE).toContain("library.cannotRebuild");
  });
});
