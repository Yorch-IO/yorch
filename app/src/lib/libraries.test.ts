/**
 * What a library is called on screen.
 *
 * The picker rendered `l.id` and nothing else, so fixing the *write* — the
 * ingest passed the library id as the library's name, which is why every row in
 * this installation's catalog is named after itself — would have left the user
 * looking at exactly the same string. Both halves or neither.
 *
 * Asserted on the function rather than through a rendered `<select>` for the
 * reason `radial.test.ts` gives: the property is a decision about two strings.
 */
import { describe, expect, it } from "vitest";

import { libraryLabel } from "./libraries";

const row = (id: string, name: string) => ({
  id,
  name,
  language: "es",
  documents: 0,
  indexedVersions: 0,
});

describe("what a library is called", () => {
  it("shows the name, and keeps the id a person has to type elsewhere", () => {
    expect(libraryLabel(row("lib_teologia", "Teología"))).toBe("Teología (lib_teologia)");
  });

  it("does not say the id twice for a library named after itself", () => {
    // Every library indexed before the ingest stopped passing the id as the
    // name — which is both of the ones on this machine.
    expect(libraryLabel(row("lib_teologia", "lib_teologia"))).toBe("lib_teologia");
  });

  it("falls back to the id rather than rendering an empty option", () => {
    expect(libraryLabel(row("lib_x", ""))).toBe("lib_x");
    expect(libraryLabel(row("lib_x", "   "))).toBe("lib_x");
  });
});
