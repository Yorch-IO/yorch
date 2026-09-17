import { useState } from "react";
import { useTranslation } from "react-i18next";
import { openUrl } from "@tauri-apps/plugin-opener";

import { Locator } from "./Locator";
import { api, errorMessage } from "./lib/api";

/** The separator `graph.projection._locator` joins a locator's parts with. */
const SEP = " · ";

/** `hh:mm:ss` or `mm:ss` at the head of a timed locator, as seconds. */
export function startSeconds(locator: string): number | null {
  const head = locator.split(SEP)[0] ?? "";
  const m = head.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return null;
  const [, a, b, c] = m;
  return c === undefined
    ? Number(a) * 60 + Number(b)
    : Number(a) * 3600 + Number(b) * 60 + Number(c);
}

/** A locator names a bucket recording when its second fact is an `s3://` URL. */
export const isBucketLocator = (locator: string): boolean =>
  locator.split(SEP).some((part) => part.startsWith("s3://"));

/**
 * A citation's locator, and — for a recording out of a bucket — a button that
 * opens the audio at the cited second.
 *
 * A bucket chunk's locator is `hh:mm:ss · s3://bucket/key`: two facts that
 * cannot move, and neither of them something a browser opens. The link that
 * does open is presigned **on click**, never stored: it dies with the
 * assumed-role session that signed it, so a link written into the answer
 * would be dead by the time a person came back to it. The manifest's own feed
 * URL rides beside it, because that one never expires.
 *
 * Every other locator renders exactly as `Locator` renders it.
 */
export function MediaLocator({
  locator,
  libraryId,
  documentId,
  className = "locator",
}: {
  locator: string;
  libraryId: string | null;
  documentId: string | undefined;
  className?: string;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [feed, setFeed] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!isBucketLocator(locator) || !libraryId || !documentId) {
    return <Locator text={locator} className={className} />;
  }
  const at = startSeconds(locator) ?? 0;

  const play = async () => {
    setBusy(true);
    setError(null);
    try {
      const link = await api.mediaLink(libraryId, documentId, at);
      if (link.sourceUrl) setFeed(link.sourceUrl);
      await openUrl(link.url).catch(() => {});
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className={className}>
      <Locator text={locator} className="" />
      {" · "}
      <button type="button" className="link" disabled={busy} onClick={() => void play()}>
        {busy ? t("ask.mediaOpening") : t("ask.mediaPlay")}
      </button>
      {feed && (
        <>
          {" · "}
          <button type="button" className="link" onClick={() => void openUrl(feed).catch(() => {})}>
            {t("ask.mediaFeed")}
          </button>
        </>
      )}
      {error && <span className="warn-inline"> {error}</span>}
    </span>
  );
}
