import { beforeEach, describe, expect, it } from "vitest";

import {
  ASK_EFFORTS,
  DEFAULT_ASK_EFFORT,
  isAskEffort,
  loadEffort,
  saveEffort,
} from "./askEffort";

beforeEach(() => {
  window.localStorage.clear();
});

describe("the level list", () => {
  it("contains the default", () => {
    // Otherwise the control opens on a value none of its radios can show, and
    // the screen renders with nothing selected.
    expect(ASK_EFFORTS).toContain(DEFAULT_ASK_EFFORT);
  });

  it("names the level that reproduces what the product did before it existed", () => {
    // Kept equal to `DEFAULT_EFFORT` in the worker's `answering/effort.py`. If
    // the two ever disagree, a user who never touches the control gets
    // something other than what they have always had — silently, because both
    // values are perfectly valid on their own.
    expect(DEFAULT_ASK_EFFORT).toBe("standard");
  });

  it("recognises only the levels the server knows", () => {
    for (const level of ASK_EFFORTS) expect(isAskEffort(level)).toBe(true);
    for (const bad of ["", "STANDARD", "exhaustivo", null, undefined, 3]) {
      expect(isAskEffort(bad)).toBe(false);
    }
  });
});

describe("remembering the level", () => {
  it("comes back on the next visit", () => {
    saveEffort("thorough", "local");
    expect(loadEffort("local")).toBe("thorough");
  });

  it("falls back rather than trusting what it read", () => {
    // A value written by an older build, or edited by hand. Handing it back to
    // the server would fail the request's validation, and the user would see a
    // question refused with no idea why — so it is checked on the way out of
    // storage, not on the way in.
    window.localStorage.setItem("companyBrain.askEffort", "exhaustivo");
    expect(loadEffort("local")).toBe(DEFAULT_ASK_EFFORT);
  });

  it("is the default when nothing was ever stored", () => {
    expect(loadEffort("local")).toBe(DEFAULT_ASK_EFFORT);
  });

  it("keeps the local plane's key bare and scopes every other", () => {
    // The same rule `scopedKey` applies to the history: scoping the local plane
    // unconditionally would orphan what is already stored, while two people
    // signed into the paid plane must not inherit each other's preference.
    saveEffort("brief", "local");
    expect(window.localStorage.getItem("companyBrain.askEffort")).toBe("brief");

    saveEffort("thorough", "cloud|https://x|tnt_1|a@b.c");
    expect(
      window.localStorage.getItem("companyBrain.askEffort.cloud|https://x|tnt_1|a@b.c"),
    ).toBe("thorough");
    // The point of the two assertions above: the planes must not collide.
    expect(loadEffort("local")).toBe("brief");
  });

  it("degrades to the default when storage throws outright", () => {
    // Some webview configurations deny storage entirely. Not remembering a
    // preference is acceptable; a blank Ask screen is not.
    const original = window.localStorage.getItem;
    window.localStorage.getItem = () => {
      throw new Error("denied");
    };
    try {
      expect(loadEffort("local")).toBe(DEFAULT_ASK_EFFORT);
    } finally {
      window.localStorage.getItem = original;
    }
  });
});
