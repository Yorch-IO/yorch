/**
 * The selected library, shared by every screen.
 *
 * It lives here rather than in each screen for two reasons, both of them bugs
 * that were shipped:
 *
 * 1. Every screen opened on a hardcoded `lib_1` that no installation has ever
 *    had, so a new user's first sight of the app was an empty shelf on Library,
 *    an empty tree on Explore, and `off_corpus` on Ask — three symptoms of one
 *    cause, none of them pointing at it.
 * 2. Each screen kept its own copy, so picking a library on Explore and then
 *    asking a question about it meant typing the id again, correctly, twice.
 *
 * The choice is persisted, because re-picking a library on every launch is the
 * same annoyance one step removed. It is validated against the list on load:
 * a remembered id whose library was deleted must not silently produce an empty
 * screen again.
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

import { api, errorMessage, type LibraryRow } from "./api";

const REMEMBERED = "companyBrain.libraryId";

/** localStorage throws outright in some webview configurations, so every access
 *  is guarded and a failure degrades to "nothing remembered" rather than a
 *  blank screen. */
function remembered(): string | null {
  try {
    return window.localStorage.getItem(REMEMBERED);
  } catch {
    return null;
  }
}

function remember(id: string): void {
  try {
    window.localStorage.setItem(REMEMBERED, id);
  } catch {
    /* not worth telling the user about */
  }
}

interface LibrariesState {
  rows: LibraryRow[];
  /** Empty until the list has loaded and something was chosen. */
  selected: string;
  select: (id: string) => void;
  reload: () => Promise<void>;
  loading: boolean;
  error: unknown;
}

const Ctx = createContext<LibrariesState | null>(null);

export function LibrariesProvider({ children }: { children: ReactNode }) {
  const [rows, setRows] = useState<LibraryRow[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const { libraries } = await api.libraries();
      setRows(libraries);
      setSelected((current) => {
        // Keep an explicit choice if it still exists; otherwise fall back to
        // what was remembered, and only then to the first row. The API orders
        // by indexed content, so the first row is the one that can answer.
        const ids = new Set(libraries.map((l) => l.id));
        if (current && ids.has(current)) return current;
        const saved = remembered();
        if (saved && ids.has(saved)) return saved;
        return libraries[0]?.id ?? "";
      });
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);

  // One fetch for the whole app. The stack may not be up yet on first paint,
  // in which case this fails and the picker offers a retry rather than the
  // screens each failing on their own.
  useEffect(() => {
    void reload();
  }, [reload]);

  const select = useCallback((id: string) => {
    setSelected(id);
    remember(id);
  }, []);

  const value = useMemo<LibrariesState>(
    () => ({ rows, selected, select, reload, loading, error }),
    [rows, selected, select, reload, loading, error],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLibraries(): LibrariesState {
  const state = useContext(Ctx);
  if (state === null) {
    throw new Error("useLibraries outside LibrariesProvider");
  }
  return state;
}

/**
 * The picker itself. It is rendered once, by the app shell's top bar, because
 * the selection it edits is one piece of state shared by every screen — five
 * copies of the same control were five ways to reach one value.
 *
 * It shows the counts because "which library" and "is this library ready" are
 * the same question in practice: a library with documents but no indexed
 * version is the single most likely reason a question comes back `off_corpus`.
 *
 * `compact` lays it out along a bar instead of stacking as a block field.
 * Nothing is dropped in that form: the counts and the not-ready warning are the
 * reason it shows a `<select>` rather than an id.
 */
/**
 * What to call a library in the picker.
 *
 * The name when there is one, and the id when the name *is* the id — which is
 * every library indexed before `ensure_library` stopped being handed the id in
 * both positions. Showing "lib_teologia · lib_teologia" would be worse than
 * showing the id once, and dropping the id entirely would take away the value
 * a person has to type into a runbook or a curl.
 *
 * Exported and tested on its own for the reason `lib/radial.ts` gives: the
 * property is a decision about two strings, and asserting it directly beats
 * rendering a `<select>` to find out which one came back.
 */
export function libraryLabel(library: LibraryRow): string {
  const named = library.name.trim();
  return named === "" || named === library.id ? library.id : `${named} (${library.id})`;
}

export function LibraryPicker({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const { rows, selected, select, reload, loading, error } = useLibraries();

  if (error !== null) {
    return (
      <p className="warn">
        {t("libraries.failed")} — {errorMessage(error)}{" "}
        <button type="button" onClick={() => void reload()}>
          {t("libraries.retry")}
        </button>
      </p>
    );
  }

  if (loading && rows.length === 0) {
    return <p className="muted">{t("libraries.loading")}</p>;
  }

  if (rows.length === 0) {
    return <p className="warn">{t("libraries.none")}</p>;
  }

  const current = rows.find((l) => l.id === selected);

  return (
    <label className={compact ? "field field-inline" : "field"}>
      <span>{t("libraries.label")}</span>
      <select value={selected} onChange={(e) => select(e.target.value)}>
        {rows.map((l) => (
          <option key={l.id} value={l.id}>
            {libraryLabel(l)} ·{" "}
            {t("libraries.counts", {
              documents: l.documents,
              indexed: l.indexedVersions,
            })}
          </option>
        ))}
      </select>
      {current !== undefined && current.indexedVersions === 0 && (
        <small className="warn">{t("libraries.notReady")}</small>
      )}
    </label>
  );
}
