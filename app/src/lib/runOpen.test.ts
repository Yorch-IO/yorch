/**
 * Where a run opens.
 *
 * The decision is worth asserting on its own because the failure it fixes was
 * a button that existed and worked — it navigated — and landed on a screen
 * that could not show what it promised.
 */
import { describe, expect, it } from "vitest";

import { channelIdFor } from "./channel";
import { runDestination } from "./runOpen";

const KNOWN = new Set(["UC-Wxkyg4RM5A6lhyXTUJP9w"]);

describe("channelIdFor", () => {
  it("is the inverse of the worker's library_id_for", () => {
    expect(channelIdFor("lib_yt_UC-Wxkyg4RM5A6lhyXTUJP9w")).toBe("UC-Wxkyg4RM5A6lhyXTUJP9w");
  });

  it("keeps the case, because a channel id is case-sensitive base64", () => {
    // Folding it would name a different channel: a 404 if you are lucky, and
    // somebody else's channel if you are not.
    expect(channelIdFor("lib_yt_UCaBcD")).toBe("UCaBcD");
  });

  it("is null for a library that is not a channel's, and for a bare prefix", () => {
    expect(channelIdFor("lib_teologia")).toBeNull();
    expect(channelIdFor("lib_yt_")).toBeNull();
    expect(channelIdFor(null)).toBeNull();
  });
});

describe("runDestination", () => {
  it("opens a channel's video on the Channel tab, with that channel selected", () => {
    // Its row, its cost and its checkbox are there, and approving it is one
    // tick among the batch it came from.
    expect(runDestination("lib_yt_UC-Wxkyg4RM5A6lhyXTUJP9w", KNOWN)).toEqual({
      tab: "channel",
      channelId: "UC-Wxkyg4RM5A6lhyXTUJP9w",
      libraryId: "lib_yt_UC-Wxkyg4RM5A6lhyXTUJP9w",
    });
  });

  it("opens everything else on the import queue, with its library selected", () => {
    // Selecting it is the whole fix: the queue is narrowed by that choice, so
    // navigating without it lands on a list that cannot hold the run.
    expect(runDestination("lib_teologia", KNOWN)).toEqual({
      tab: "import",
      channelId: null,
      libraryId: "lib_teologia",
    });
  });

  it("falls back to the queue for a channel this installation never synced", () => {
    // The Channel screen would answer `channel_not_synced`; the queue can show
    // the run from its library id alone.
    expect(runDestination("lib_yt_UCneverSynced", KNOWN)).toEqual({
      tab: "import",
      channelId: null,
      libraryId: "lib_yt_UCneverSynced",
    });
  });

  it("changes no selection for a run the catalog cannot place", () => {
    expect(runDestination(null, KNOWN)).toEqual({
      tab: "import",
      channelId: null,
      libraryId: null,
    });
  });
});
