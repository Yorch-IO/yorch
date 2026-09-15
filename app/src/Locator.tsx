import { openUrl } from "@tauri-apps/plugin-opener";

/** The separator `graph.projection._locator` joins a locator's parts with. */
const SEP = " · ";

const isUrl = (part: string) =>
  part.startsWith("https://") || part.startsWith("http://");

/**
 * A citation's locator, with any URL in it made openable.
 *
 * Every locator is a ` · `-joined string of whatever positional facts the source
 * had — a title, a page, a section, a byte range. A *video's* is two facts that
 * cannot move: when it was said, and a link that opens the video at that second.
 * Rendering that as inert text, which is what every site did before this,
 * printed an address nobody could follow.
 *
 * **A button, not an `<a href>`.** The webview holds a `default-src 'self'` CSP
 * with no exception, so an anchor would either be blocked or navigate the app
 * away from itself; `openUrl` hands it to the system browser instead. The
 * capability for it is already granted, and it needs *two* entries to work —
 * `opener:allow-open-url` permits the call and `opener:allow-default-urls`
 * supplies the scope it is checked against, and granting only the first denies
 * every URL at runtime with no error anybody can see.
 *
 * The button's label is the URL itself, so a host where `openUrl` does nothing —
 * a browser, a test — still shows an address that can be read and copied.
 */
export function Locator({
  text,
  className = "locator",
}: {
  text: string;
  className?: string;
}) {
  if (!text) return null;
  const parts = text.split(SEP);
  if (!parts.some(isUrl)) return <span className={className}>{text}</span>;

  return (
    <span className={className}>
      {parts.map((part, i) => (
        <span key={`${i}:${part}`}>
          {i > 0 ? SEP : ""}
          {isUrl(part) ? (
            <button
              type="button"
              className="link"
              // Failure is not worth an error panel over: the address is the
              // label, so a viewer who cannot be handed to a browser can still
              // read and copy it.
              onClick={() => void openUrl(part).catch(() => {})}
            >
              {part}
            </button>
          ) : (
            part
          )}
        </span>
      ))}
    </span>
  );
}
