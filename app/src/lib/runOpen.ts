/**
 * Where a run opens, and what has to be selected there first.
 *
 * Pure, and its own module for the reason `channel.ts` and `radial.ts` are:
 * what is worth asserting is the *routing decision*, and a rendered test can
 * only say that a button exists. The defect this exists to stop was exactly a
 * button that existed: the activity indicator's "Abrir" went to the import
 * queue without touching the library selection, and that queue is filtered by
 * it — so the one affordance the product offered for looking at a parked run
 * led to a screen that could not show it. Found on a channel's probes, where
 * it is guaranteed: a channel's runs live in `lib_yt_<channelId>` and the
 * picker is never on that library, because the Channel tab does not use it.
 */
import { channelIdFor } from "./channel";
import type { Tab } from "../App";

export interface RunDestination {
  tab: Tab;
  /** Select this channel before showing the tab, or null to leave it alone. */
  channelId: string | null;
  /** Select this library before showing the tab, or null to leave it alone. */
  libraryId: string | null;
}

/**
 * The screen that owns a run.
 *
 * A video probed from a channel belongs to the Channel tab: that is where its
 * row, its cost and its checkbox are, and where approving it is one tick among
 * the batch it came from. Everything else belongs to the import queue.
 *
 * `knownChannels` is what the picker can actually select. A channel library
 * whose catalogue this installation has never synced — another machine's, or
 * one whose files were removed — would send the Channel screen to a
 * `channel_not_synced` 404, so those fall back to the queue, which can show
 * the run by library id alone.
 */
export function runDestination(
  libraryId: string | null,
  knownChannels: ReadonlySet<string>,
): RunDestination {
  const channelId = channelIdFor(libraryId);
  if (channelId !== null && knownChannels.has(channelId)) {
    return { tab: "channel", channelId, libraryId };
  }
  return { tab: "import", channelId: null, libraryId };
}
