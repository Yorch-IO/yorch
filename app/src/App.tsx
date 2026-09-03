import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { LANGUAGES, setLanguage, type Language } from "./i18n";
import { BackendProvider, useBackend } from "./lib/backend";
import { LibrariesProvider, LibraryPicker } from "./lib/libraries";
import { ActivityIndicator } from "./ActivityIndicator";
import { AskScreen } from "./screens/AskScreen";
import { ImportScreen } from "./screens/ImportScreen";
import { ExploreScreen } from "./screens/ExploreScreen";
import { HomeScreen } from "./screens/HomeScreen";
import { GraphScreen } from "./screens/GraphScreen";
import { LibraryScreen } from "./screens/LibraryScreen";
import { StackScreen } from "./screens/StackScreen";

/** Ordered as the work is: see what the project holds, bring the stack up, see
 *  what is on the shelf, look inside a document, put more in, then ask about
 *  them. Home is first and is the default, because "what is in here?" is the
 *  question a person arrives with — Services is a diagnostic panel and was only
 *  ever the landing screen for want of anything else. */
const TABS = [
  "home",
  "stack",
  "library",
  "explore",
  "graph",
  "import",
  "ask",
] as const;
export type Tab = (typeof TABS)[number];

/** Every screen may take a `go` and an `active`, and each is read by one screen
 *  — a screen declaring no parameters is assignable to this, so the rest are
 *  unchanged. Home needs `go` because the stack being down is a state it reports
 *  with a way out of it, and the way out is the Services tab. Graph needs
 *  `active` because all seven of these are mounted at startup: without it the
 *  library graph downloads its envelope and lays it out on a tab nobody has
 *  opened, which on the real corpus is 183 ms of query and up to 3.9 s of
 *  simulation. It latches there — arriving is the trigger, and leaving must
 *  never throw work away. */
const SCREENS: Record<
  Tab,
  (props: { go: (tab: Tab) => void; active: boolean }) => JSX.Element
> = {
  home: HomeScreen,
  stack: StackScreen,
  library: LibraryScreen,
  explore: ExploreScreen,
  graph: GraphScreen,
  import: ImportScreen,
  ask: AskScreen,
};

/** Home and Services are the two screens that own no library. Services is what
 *  you use before there is anything to pick; Home's figures are project-wide, so
 *  a picker there would offer a choice that changes nothing on the screen. The
 *  bar itself still renders on both, so switching does not shift everything
 *  below by the height of a form field. */
const NEEDS_LIBRARY: ReadonlySet<Tab> = new Set<Tab>([
  "library",
  "explore",
  "graph",
  "import",
  "ask",
]);

/** The provider has to sit above what it invalidates, and `App` is what reads
 *  the identity, so the two cannot be the same component. */
export default function App() {
  return (
    <BackendProvider>
      <Shell />
    </BackendProvider>
  );
}

function Shell() {
  const { identity } = useBackend();
  const { t, i18n } = useTranslation();
  const [tab, setTab] = useState<Tab>("home");
  const content = useRef<HTMLElement>(null);

  // One scroll container for seven screens, so the scroll position of the screen
  // you left is still applied to the one you arrived at. Switching from a long
  // Library list to Ask would open it halfway down.
  //
  // Assigning `scrollTop` rather than calling `scrollTo`: it is a property on
  // every element rather than a method some environments leave out, which the
  // shell test caught by throwing on the first render.
  useEffect(() => {
    if (content.current) content.current.scrollTop = 0;
  }, [tab]);

  return (
    <LibrariesProvider>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">
            <h1>{t("app.name")}</h1>
            <p className="tagline">{t("app.tagline")}</p>
          </div>

          <nav className="tabs">
            {TABS.map((name) => (
              <button
                key={name}
                type="button"
                className={name === tab ? "active" : ""}
                onClick={() => setTab(name)}
              >
                {t(`nav.${name}`)}
              </button>
            ))}
          </nav>

          {/* Below the tabs, not above them: it is news, not navigation, and it
              is absent most of the time — anchoring it under a fixed list keeps
              the tabs from moving when a run starts. */}
          <ActivityIndicator go={setTab} />
        </aside>

        <div className="workspace">
          <header className="topbar">
            {/* One picker for the whole app, because one library is what the
                whole app is looking at. It used to be rendered by each of the
                five screens that act on a library, over this same shared
                state — so Ask would have shown it twice once its own left
                column arrived. */}
            <div className="topbar-library">
              {NEEDS_LIBRARY.has(tab) && <LibraryPicker compact />}
            </div>

            <label className="language">
              <span>{t("nav.language")}</span>
              <select
                value={i18n.resolvedLanguage}
                onChange={(e) => setLanguage(e.target.value as Language)}
              >
                {LANGUAGES.map((code) => (
                  <option key={code} value={code}>
                    {code.toUpperCase()}
                  </option>
                ))}
              </select>
            </label>
          </header>

          {/*
        Every screen stays mounted and the inactive ones are hidden, rather than
        rendering only the active one. Swapping the component type unmounts the
        old screen, and React throws away all of its state with it: a question
        and its answer, a typed path, an opened document, the library you picked.
        Leaving a tab was indistinguishable from pressing a reset button.

        It also fixes a worse case. `ImportScreen` polls the approval gate on an
        interval it cleans up on unmount, so switching tabs during an import
        silently stopped watching a run that was still going — and the gate it
        was waiting for expires as a rejection after seven days.

        The cost is that all six mount at startup, so the screens that
        load on mount each make one local, free call. That is cheaper than the
        bug, and it means the shelf is already warm when you switch to it.

        Nothing here may set `display` on these wrappers. `[hidden]` is a
        user-agent rule at the lowest specificity there is, so a single
        `main > div { display: … }` would render all seven screens at once.
      */}
          <main className="content" ref={content}>
            {TABS.map((name) => {
              const Screen = SCREENS[name];
              // Changing plane changes what every one of these can read, and a
              // screen that stays mounted keeps what it read from the other one
              // — Home's project-wide figures, the graph's laid-out canvas,
              // Import's poll of a run belonging to a different API process.
              // Putting the identity in the key remounts them, which is the
              // same reset the comment above is careful *not* to do on a tab
              // change, and is right here for the opposite reason: leaving a
              // tab must not throw work away, and switching plane must.
              //
              // Services is deliberately excluded. It is the screen holding the
              // switch, so remounting it under the user's own hand would take
              // away the draft they just saved, the error panel and the ping
              // they are reading; `refresh()` there already re-reads everything
              // that went stale.
              const key = name === "stack" ? name : `${identity}:${name}`;
              return (
                <div key={key} hidden={name !== tab}>
                  <Screen go={setTab} active={name === tab} />
                </div>
              );
            })}
          </main>
        </div>
      </div>
    </LibrariesProvider>
  );
}
