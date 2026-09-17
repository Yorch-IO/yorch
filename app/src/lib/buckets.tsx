/**
 * The registered buckets, for the whole app, and the picker in the top bar.
 *
 * `lib/channels.tsx` with a bucket in it, and the same three decisions apply:
 * one fetch for the whole app, again on every change of plane, with the
 * selection dropped *before* the fetch; the selection remembered per identity
 * in `localStorage`, guarded; and the thing you do once per source — here a
 * re-sync — in the bar beside the list it feeds, because it does not deserve a
 * screenful.
 *
 * What is *not* in the bar, unlike the channel's URL field: registering a
 * bucket takes a name, a prefix, a role ARN and a manifest mapping, which is a
 * form and not a field. The screen holds it.
 *
 * **Paid plane only.** In local mode the list call answers 404, which the
 * proxy turns into `control_status`; the provider treats that as "this plane
 * has no buckets" rather than as an error, so the tab renders its explanation
 * instead of a red panel.
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

import {
  api,
  errorGuidanceKey,
  errorMessage,
  isAppError,
  type BucketSource,
  type BucketSynced,
  type StoredBucket,
} from "./api";
import { scopedKey, useBackend } from "./backend";
import { useLibraries } from "./libraries";

const REMEMBERED = "companyBrain.bucketId";

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

/** A 404 on the list is the local plane saying it serves no bucket route.
 *
 *  Read off the message — "the control API returned 404: …" — because the
 *  status is not a field of `AppError`, and off the *absence* of a control
 *  kind: FastAPI's own not-found body is bare `{"detail": "Not Found"}` with
 *  no `kind`, where every refusal a bucket route makes carries one. */
export function unserved(e: unknown): boolean {
  return (
    isAppError(e) &&
    e.kind === "control_status" &&
    e.controlKind === undefined &&
    /\breturned 40[45]\b/.test(e.message)
  );
}

interface BucketsState {
  rows: StoredBucket[];
  selected: string;
  select: (id: string) => void;
  reload: () => Promise<void>;
  /** Register a bucket and catalogue it. Free. Resolves the sync's counts, or
   *  null when it failed — the error is in `error`, and the caller keeps what
   *  the person typed so it can be corrected rather than retyped. */
  register: (
    source: BucketSource,
    libraryName?: string,
    libraryId?: string,
  ) => Promise<BucketSynced | null>;
  /** Walk the selected bucket's listing again. Free. */
  refresh: () => Promise<BucketSynced | null>;
  forget: (id: string) => Promise<boolean>;
  syncing: boolean;
  news: BucketSynced | null;
  loading: boolean;
  /** True when this plane serves no bucket route at all — local mode. */
  unavailable: boolean;
  error: unknown;
}

const Ctx = createContext<BucketsState | null>(null);

export function BucketsProvider({ children }: { children: ReactNode }) {
  const { identity } = useBackend();
  const libraries = useLibraries();
  const [rows, setRows] = useState<StoredBucket[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [news, setNews] = useState<BucketSynced | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { buckets } = await api.buckets();
      setUnavailable(false);
      setRows(buckets);
      setSelected((current) => {
        const ids = new Set(buckets.map((b) => b.bucketId));
        if (current && ids.has(current)) return current;
        const saved = remembered(identity);
        if (saved && ids.has(saved)) return saved;
        return buckets[0]?.bucketId ?? "";
      });
    } catch (e) {
      if (unserved(e)) {
        setUnavailable(true);
        setRows([]);
      } else {
        setError(e);
      }
    } finally {
      setLoading(false);
    }
  }, [identity]);

  useEffect(() => {
    setSelected("");
    setNews(null);
    void reload();
  }, [identity, reload]);

  const select = useCallback(
    (id: string) => {
      setSelected(id);
      remember(id, identity);
    },
    [identity],
  );

  const register = useCallback(
    async (
      source: BucketSource,
      libraryName = "",
      libraryId = "",
    ): Promise<BucketSynced | null> => {
      setSyncing(true);
      setError(null);
      setNews(null);
      try {
        const { synced } = await api.bucketRegister(source, libraryName, libraryId);
        await reload();
        select(synced.bucketId);
        setNews(synced);
        // The sync created the bucket's library; the shelf has to show it.
        void libraries.reload();
        return synced;
      } catch (e) {
        await reload();
        setError(e);
        return null;
      } finally {
        setSyncing(false);
      }
    },
    [reload, select, libraries],
  );

  const refresh = useCallback(async (): Promise<BucketSynced | null> => {
    if (!selected) return null;
    setSyncing(true);
    setError(null);
    setNews(null);
    try {
      const { synced } = await api.bucketSync(selected);
      await reload();
      setNews(synced);
      return synced;
    } catch (e) {
      await reload();
      setError(e);
      return null;
    } finally {
      setSyncing(false);
    }
  }, [selected, reload]);

  const forget = useCallback(
    async (id: string): Promise<boolean> => {
      setError(null);
      try {
        await api.bucketForget(id);
        await reload();
        return true;
      } catch (e) {
        setError(e);
        return false;
      }
    },
    [reload],
  );

  const value = useMemo<BucketsState>(
    () => ({
      rows, selected, select, reload, register, refresh, forget, syncing, news, loading,
      unavailable, error,
    }),
    [rows, selected, select, reload, register, refresh, forget, syncing, news, loading,
     unavailable, error],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useBuckets(): BucketsState {
  const state = useContext(Ctx);
  if (state === null) {
    throw new Error("useBuckets outside BucketsProvider");
  }
  return state;
}

/**
 * The picker and the re-sync, on one line, for the top bar.
 *
 * An empty list is not an error: it is the ordinary state before the first
 * bucket is registered, and the screen's form is how you leave it. A plane
 * that serves no bucket route renders nothing here at all — the screen says
 * why.
 */
export function BucketPicker() {
  const { t } = useTranslation();
  const { rows, selected, select, reload, refresh, syncing, news, loading, unavailable, error } =
    useBuckets();
  if (unavailable) return null;
  const current = rows.find((b) => b.bucketId === selected);

  return (
    <div className="channel-bar">
      {rows.length > 0 && (
        <label className="field field-inline">
          <span>{t("bucket.pick")}</span>
          <select value={selected} onChange={(e) => select(e.target.value)}>
            {rows.map((b) => (
              <option key={b.bucketId} value={b.bucketId}>
                {b.libraryName} · {t("bucket.objectCount", { count: b.objectCount })}
              </option>
            ))}
          </select>
        </label>
      )}
      {current && (
        <span className="channel-refresh">
          <button
            type="button"
            disabled={syncing}
            title={t("bucket.refreshCaveat")}
            onClick={() => void refresh()}
          >
            {syncing ? t("bucket.syncing") : t("bucket.refresh")}
          </button>
        </span>
      )}
      {loading && rows.length === 0 && error === null && (
        <span className="muted">{t("bucket.loading")}</span>
      )}
      {error !== null && (
        <p className="warn">
          {errorGuidanceKey(error) ? `${t(errorGuidanceKey(error)!)} ` : ""}
          {errorMessage(error)}{" "}
          <button type="button" onClick={() => void reload()}>
            {t("libraries.retry")}
          </button>
        </p>
      )}
      {error === null && news !== null && news.bucketId === selected && (
        <span className="muted channel-news">
          {t("bucket.synced", { objects: news.objects, added: news.added, changed: news.changed })}
          {news.absent > 0 && ` · ${t("bucket.syncedAbsent", { count: news.absent })}`}
          {news.estimated > 0 && ` · ${t("bucket.syncedEstimated", { count: news.estimated })}`}
        </span>
      )}
    </div>
  );
}
