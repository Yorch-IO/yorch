/**
 * The selected channel, shared between the app shell's top bar and the Channel
 * screen.
 *
 * It lives here rather than in the screen for the reason `libraries.tsx` gives
 * for the library: the control that edits it renders in the shell's top bar,
 * in the slot the library picker uses on every other tab, and the screen reads
 * it. A channel **is** a library (`lib_yt_<channelId>`), so this is not a
 * second picker beside the library one — it is the library picker in the form
 * that tab needs, showing a title a person can read rather than an id nobody
 * can.
 *
 * Syncing lives here too, because a sync is what puts a channel in the list
 * and the list is what the picker shows. It also creates the channel's library
 * on the server, so a sync reloads the libraries: the picker on the other tabs
 * must see the new shelf without a relaunch.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";

import { api, errorGuidanceKey, errorMessage, type ChannelSummary } from "./api";
import { scopedKey, useBackend } from "./backend";
import { useLibraries } from "./libraries";

const REMEMBERED = "companyBrain.channelId";

/** The catalogue ceiling, not the screen's old 100: the catalogue is a free
 *  artefact worth having in full while the *preselection* is what costs money
 *  and is capped separately. */
export const SYNC_LIMIT = 500;

/** localStorage throws outright in some webview configurations, so every access
 *  is guarded and a failure degrades to "nothing remembered". */
function remembered(identity: string): string | null {
  try {
    return window.localStorage.getItem(scopedKey(REMEMBERED, identity));
  } catch {
    return null;
  }
}

function remember(id: string, identity: string): void {
  try {
    window.localStorage.setItem(scopedKey(REMEMBERED, identity), id);
  } catch {
    /* not worth telling the user about */
  }
}

interface ChannelsState {
  rows: ChannelSummary[];
  /** Empty until the list has loaded and something was chosen. */
  selected: string;
  select: (id: string) => void;
  reload: () => Promise<void>;
  /** Catalogue a channel and select it. Free: quota, never money. Resolves
   *  `false` when it failed — the error is in `error`, and the caller keeps
   *  what the person typed so it can be corrected rather than retyped. */
  sync: (url: string) => Promise<boolean>;
  syncing: boolean;
  loading: boolean;
  error: unknown;
}

const Ctx = createContext<ChannelsState | null>(null);

export function ChannelsProvider({ children }: { children: ReactNode }) {
  const { identity } = useBackend();
  const libraries = useLibraries();
  const [rows, setRows] = useState<ChannelSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { channels } = await api.channels();
      setRows(channels);
      setSelected((current) => {
        // Keep an explicit choice if it still exists; otherwise fall back to
        // what was remembered, and only then to the first row, which the API
        // orders most recently synced first.
        const ids = new Set(channels.map((c) => c.channel.channelId));
        if (current && ids.has(current)) return current;
        const saved = remembered(identity);
        if (saved && ids.has(saved)) return saved;
        return channels[0]?.channel.channelId ?? "";
      });
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [identity]);

  // One fetch for the whole app, again on every change of plane, with the
  // selection dropped *before* the fetch — the reason `LibrariesProvider`
  // records: an id that exists in both planes is precisely the case that must
  // not be silently carried across.
  useEffect(() => {
    setSelected("");
    void reload();
  }, [identity, reload]);

  const select = useCallback(
    (id: string) => {
      setSelected(id);
      remember(id, identity);
    },
    [identity],
  );

  const sync = useCallback(
    async (url: string): Promise<boolean> => {
      setSyncing(true);
      setError(null);
      try {
        const summary = await api.channelSync(url.trim(), SYNC_LIMIT);
        await reload();
        select(summary.channel.channelId);
        // The sync created the channel's library; the shelf has to show it.
        void libraries.reload();
        return true;
      } catch (e) {
        setError(e);
        return false;
      } finally {
        setSyncing(false);
      }
    },
    [reload, select, libraries],
  );

  const value = useMemo<ChannelsState>(
    () => ({ rows, selected, select, reload, sync, syncing, loading, error }),
    [rows, selected, select, reload, sync, syncing, loading, error],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useChannels(): ChannelsState {
  const state = useContext(Ctx);
  if (state === null) {
    throw new Error("useChannels outside ChannelsProvider");
  }
  return state;
}

/**
 * The picker and the sync field, on one line, for the top bar.
 *
 * The sync used to be the first panel of the Channel screen, a screenful of
 * controls for the thing you do once per channel. Here it is a field and a
 * button beside the list it feeds. The caveat — quota, never money — rides as
 * the field's title, because a bar has no room for a paragraph and the one
 * fact that matters at the moment of pressing is that nothing is spent.
 *
 * An empty list is not an error: it is the ordinary state before the first
 * sync, and the field is how you leave it. A failed list *is* reported, with
 * the guidance the error's kind carries — `youtube_key_missing` points at the
 * Services screen, which is where the key goes.
 */
export function ChannelPicker() {
  const { t } = useTranslation();
  const { rows, selected, select, reload, sync, syncing, loading, error } = useChannels();
  const [url, setUrl] = useState("");

  const submit = async () => {
    if (url.trim() === "") return;
    if (await sync(url)) setUrl("");
  };

  return (
    <div className="channel-bar">
      {rows.length > 0 && (
        <label className="field field-inline">
          <span>{t("channel.pick")}</span>
          <select value={selected} onChange={(e) => select(e.target.value)}>
            {rows.map((c) => (
              <option key={c.channel.channelId} value={c.channel.channelId}>
                {c.channel.title} · {t("channel.videoCount", { count: c.videoCount })}
              </option>
            ))}
          </select>
        </label>
      )}
      {loading && rows.length === 0 && error === null && (
        <span className="muted">{t("channel.loading")}</span>
      )}
      <form
        className="field-inline"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          type="text"
          value={url}
          placeholder={t("channel.urlPlaceholder")}
          title={t("channel.syncCaveat")}
          aria-label={t("channel.urlPlaceholder")}
          onChange={(e) => setUrl(e.target.value)}
        />
        <button type="submit" disabled={syncing || url.trim() === ""}>
          {syncing ? t("channel.syncing") : t("channel.syncStart")}
        </button>
      </form>
      {error !== null && (
        <p className="warn">
          {errorGuidanceKey(error) ? `${t(errorGuidanceKey(error)!)} ` : ""}
          {errorMessage(error)}{" "}
          <button type="button" onClick={() => void reload()}>
            {t("libraries.retry")}
          </button>
        </p>
      )}
    </div>
  );
}
