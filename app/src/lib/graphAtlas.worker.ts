/// <reference lib="webworker" />
import { atlasSteps, type AtlasMessage, type AtlasRequest } from "./graphAtlas";

/**
 * A message pump, and deliberately nothing else.
 *
 * jsdom has no `Worker`, so this is the one file in the feature that the test
 * suite cannot execute — every assertion runs `atlasSteps` through the inline
 * path instead. So this file is kept too small to be wrong: all of the
 * arithmetic, the ordering and the seeding live in `graphAtlas.ts`, which is
 * tested.
 */
self.onmessage = (event: MessageEvent<AtlasRequest>) => {
  const req = event.data;
  try {
    for (const message of atlasSteps(req)) {
      // `xy` is transferred: this side has no further use for it, and it is
      // 100 KB per layout on the real library.
      if (message.kind === "layout") {
        (self as unknown as Worker).postMessage(message, [message.xy.buffer]);
      } else {
        (self as unknown as Worker).postMessage(message);
      }
    }
  } catch (error) {
    const failed: AtlasMessage = {
      kind: "failed",
      job: req.job,
      message: String(error),
    };
    (self as unknown as Worker).postMessage(failed);
  }
};
