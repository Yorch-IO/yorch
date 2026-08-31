/**
 * Which control plane the app is pointed at, held once for the whole app.
 *
 * It lived inside `StackScreen` and nothing else could see it, which is the
 * whole of the defect this file exists to fix: switching between the local
 * stack and the paid service changed where requests went and changed nothing on
 * screen. The library picker kept the rows it had fetched at launch, so the
 * shelf, the graph and the question all still described the other plane — and a
 * remembered library id is a value two organisations pick independently
 * (`lib_teologia` is the one in this corpus), so the id validated as good and
 * the picker showed a name for a corpus that was no longer there.
 *
 * Two things follow from having it here, and both are elsewhere:
 *
 * - `App` keys the screens on `identity`, so changing planes remounts them.
 * - `libraries.tsx` and `askSession.ts` scope their stored keys on it.
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

import { api, type BackendInfo } from "./api";

/**
 * A string that changes exactly when what the app can read changes.
 *
 * The address and the organisation are in it for the obvious reason. The email
 * is in it because signing out and back in as somebody else must not leave the
 * previous person's questions in the history or their library in the picker —
 * the paid plane is multi-tenant and the window is not. Signed out of the paid
 * plane is its own identity (an empty email), which is right: nothing can be
 * read there, so nothing from there should be on screen.
 *
 * The local plane is one identity, because it is one organisation by
 * construction — the free plane *is* the legacy tenant.
 */
export function backendIdentity(info: BackendInfo): string {
  if (info.mode === "local") return "local";
  return ["cloud", info.baseUrl, info.tenantId, info.email].join("|");
}

/**
 * A localStorage key scoped to one plane — and the local plane's key is the
 * bare one, unscoped.
 *
 * That branch is the same decision as `_salt()` returning nothing for the
 * legacy tenant, for the same reason: scoping unconditionally would orphan
 * every history and every remembered library that exists today, in exchange for
 * tidiness. The local plane is what everyone has been using, so its key is what
 * it always was.
 */
export function scopedKey(base: string, identity: string): string {
  return identity === "local" ? base : `${base}.${identity}`;
}

interface BackendState {
  /** Null only before the first read has answered. */
  info: BackendInfo | null;
  /** `"local"` until then — see `load` for why that is the honest default. */
  identity: string;
  /** Publish a plane the user just chose, signed into, or signed out of.
   *  Whatever call returned the new `BackendInfo` passes it here; this is the
   *  one place that decides everything downstream is stale. */
  publish: (info: BackendInfo | null) => void;
  /** Re-read the plane from Rust. It returns what it read, because the caller
   *  that needs it — `StackScreen.refresh` — has to branch on the mode
   *  immediately and a second read would be a second chance to disagree. */
  reload: () => Promise<BackendInfo | null>;
}

const Ctx = createContext<BackendState | null>(null);

export function BackendProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<BackendInfo | null>(null);
  const [identity, setIdentity] = useState("local");

  const publish = useCallback((next: BackendInfo | null) => {
    setInfo(next);
    // A failed read is not a plane. Keep the identity that is already there
    // rather than moving everything to a scope nothing will ever fill.
    if (next) setIdentity(backendIdentity(next));
  }, []);

  const reload = useCallback(async (): Promise<BackendInfo | null> => {
    try {
      const chosen = await api.backendSettings();
      publish(chosen);
      return chosen;
    } catch {
      // Rust reads this from local state and it should not fail. If it does,
      // the local plane is the right guess: it is the default the app ships
      // with, and guessing anything else would consult a stored key that has
      // never been written and report an empty shelf as the first thing a user
      // sees.
      publish(null);
      return null;
    }
  }, [publish]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const value = useMemo<BackendState>(
    () => ({ info, identity, publish, reload }),
    [info, identity, publish, reload],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useBackend(): BackendState {
  const state = useContext(Ctx);
  if (state === null) {
    throw new Error("useBackend outside BackendProvider");
  }
  return state;
}
