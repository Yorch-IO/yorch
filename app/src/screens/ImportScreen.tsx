import { getCurrentWebview } from "@tauri-apps/api/webview";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  DEFAULT_STAGES,
  errorGuidanceKey,
  errorMessage,
  type StageOptions,
} from "../lib/api";
import { useLibraries } from "../lib/libraries";
import { useImportQueue } from "../lib/importQueue";
import { ImportQueue } from "../ImportQueue";

// `Money`, `Cost` and `range` moved to `../Money` when the audit ledger started
// rendering the same figures — the gate quotes a cost and the ledger reports
// what was billed, and the two have to be comparable at a glance. Re-exported
// because this module is where they were and tests import them from this path.
export { Cost, range } from "../Money";

// The gate moved to `../GateReview` with its profile machinery, because the
// queue can hold several runs parked at their own gates for up to seven days
// each, and each one needs its own choice of rules.
export { PROFILE_SWITCHES } from "../GateReview";

/** The last component of a path, for the two separators the app can be handed.
 *  Exported for its own test, for the reason `libraryLabel` gives: the property
 *  is a decision about a string and asserting it directly beats rendering a
 *  screen to find out. */
export function fileName(path: string): string {
  const parts = path.split(/[/\\]/).filter((p) => p !== "");
  return parts[parts.length - 1] ?? path;
}

export function ImportScreen() {
  const { t, i18n } = useTranslation();

  const { selected: libraryId } = useLibraries();
  /** Every file chosen, not just the first.
   *
   *  This screen used to keep `[first]` and count the rest as "ignored",
   *  because one screen held one run and one run is one approval gate. The
   *  queue below is what made that unnecessary: **enqueuing is starting the
   *  workflow**, the free stages cost nothing, and each run parks at its own
   *  gate until somebody answers it. So a drop of five books is five imports,
   *  approved one at a time, rather than four files that vanished. */
  const [paths, setPaths] = useState<string[]>([]);
  const [picking, setPicking] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [stages, setStages] = useState<StageOptions>(DEFAULT_STAGES);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  /** What the batch could not do, per file.
   *
   *  Kept apart from `error` because a batch is not all-or-nothing: four books
   *  can enqueue while the fifth is refused for an unsupported format, and one
   *  red panel saying "it failed" would be wrong about the four. */
  const [refused, setRefused] = useState<{ path: string; message: string }[]>([]);
  const dropZone = useRef<HTMLDivElement>(null);
  /** Whether the pointer was last seen inside the drop zone. See the drop
   *  branch below for why this cannot be the `dragging` state. */
  const inside = useRef(false);

  const { items, loaded, error: queueError, refresh, settled } =
    useImportQueue(libraryId);

  /** Accept paths from either source. Extension is not checked here: the
   *  chooser already filters, a drop cannot be filtered, and the server refuses
   *  an unsupported suffix with an error that names what it does support —
   *  which is a better message than one this screen could invent. */
  const choose = useCallback((chosen: string[]) => {
    if (chosen.length === 0) return;
    // Appended rather than replacing, so a second trip to the chooser adds to
    // the batch. Deduplicated by path, because choosing the same file twice is
    // a slip and two runs over one file would both be billed.
    setPaths((current) => [...new Set([...current, ...chosen])]);
    setError(null);
    setRefused([]);
  }, []);

  const pick = useCallback(async () => {
    setPicking(true);
    setError(null);
    try {
      const picked = await api.pickSource();
      // `null` is a dismissed dialog. Leave the previous choice alone: the
      // person opened the chooser and changed their mind, which is not a
      // reason to take away what they had already selected.
      if (picked !== null) choose([picked]);
    } catch (e) {
      setError(e);
    } finally {
      setPicking(false);
    }
  }, [choose]);

  // The webview's own drag events carry a `File` with no path — the browser
  // withholds it, and a path is the only thing either plane can use. Tauri's
  // native drag-drop is the one that reports real paths, so the listener is
  // bound to the webview rather than to a React `onDrop`.
  //
  // Bound once for the screen's lifetime, and the screen stays mounted while
  // other tabs are shown, so `over` is filtered on the pointer being inside
  // this drop zone. Without that, dragging a file anywhere over the window
  // would light up a target the person cannot see.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void (async () => {
      // The webview API is only there inside a Tauri window. Under a component
      // test — and under any host that does not provide it — subscribing
      // throws, and dropping files is a convenience: the chooser button does
      // the same job. So the screen degrades to "no drag and drop" rather than
      // to a blank panel.
      let fn: (() => void) | undefined;
      try {
        fn = await getCurrentWebview().onDragDropEvent((event) => {
          const zone = dropZone.current;
          if (!zone) return;
          if (event.payload.type === "over") {
            const { x, y } = event.payload.position;
            const box = zone.getBoundingClientRect();
            inside.current =
              x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
            setDragging(inside.current);
            return;
          }
          if (event.payload.type === "drop") {
            // The drop payload carries no position, so whether it landed on the
            // zone is decided by the last `over` — a ref rather than the state,
            // because this closure is bound once and would otherwise read the
            // value `dragging` had when it was created.
            setDragging(false);
            if (inside.current) choose(event.payload.paths);
            inside.current = false;
            return;
          }
          inside.current = false;
          setDragging(false);
        });
      } catch {
        return;
      }
      if (cancelled) fn();
      else unlisten = fn;
    })();
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [choose]);

  /** Stage and start one workflow per file, in order.
   *
   *  **Sequential, not parallel.** Staging copies or uploads the bytes, and a
   *  parallel burst buys nothing against one disk or one upstream link. A
   *  per-file failure is recorded against that file and the loop continues:
   *  refusing the whole batch because the fifth book is a `.pptx` would throw
   *  away four imports that were fine.
   *
   *  Nothing here is paid for. Each run stops at its gate, which is where the
   *  money is decided, and it waits there for up to seven days. */
  const start = useCallback(async () => {
    if (!libraryId || paths.length === 0) return;
    setBusy(true);
    setError(null);
    setRefused([]);
    const failures: { path: string; message: string }[] = [];
    const enqueued: string[] = [];
    for (const path of paths) {
      try {
        // Staging is what makes one screen serve both planes. Local mode hands
        // the path straight back; cloud mode uploads the file and returns the
        // path *inside the worker's container*, which is the only path
        // `ingestStart` can use there. The key comes back too, because the two
        // planes derive it differently and the screen should not have to know.
        const staged = await api.stageSource(path);
        await api.ingestStart(
          {
            libraryId,
            sourcePath: staged.sourcePath,
            sourceKey: staged.sourceKey,
            title: "",
          },
          stages,
        );
        enqueued.push(path);
      } catch (e) {
        failures.push({ path, message: errorMessage(e) });
      }
    }
    // Only what got through leaves the chooser, so a refused file stays visible
    // beside the reason and can be removed or retried without being re-picked.
    setPaths((current) => current.filter((p) => !enqueued.includes(p)));
    setRefused(failures);
    setBusy(false);
    void refresh();
  }, [libraryId, paths, stages, refresh]);

  /** Answer one item's gate. The queue is the source of truth, so this records
   *  nothing locally — it forgets the report and lets the next poll say what
   *  happened. */
  const decide = useCallback(
    async (workflowId: string, approved: boolean, options: StageOptions) => {
      try {
        await api.ingestApprove(workflowId, { approved, options, reason: "" });
      } catch (e) {
        setError(e);
      } finally {
        settled(workflowId);
        void refresh();
      }
    },
    [refresh, settled],
  );

  const toggle = (key: keyof StageOptions) =>
    setStages((s) => ({ ...s, [key]: !s[key] }));

  return (
    <section className="screen">
      <h2>{t("import.title")}</h2>
      <p className="intro">{t("import.intro")}</p>

      {/* No text field. A path typed by hand cannot work in either mode — the
          local worker opens `/workspace` and the cloud worker is on another
          machine — so the only paths that may reach `stage_source` are ones the
          OS produced. `aria-label` rather than a visible one: the zone's own
          text is the label, and repeating it above would read it twice. */}
      <div
        ref={dropZone}
        className={dragging ? "dropzone dragging" : "dropzone"}
        role="group"
        aria-label={t("import.file")}
      >
        {paths.length === 0 ? (
          <p className="muted">{t("import.dropHint")}</p>
        ) : (
          <ul className="chosen-files">
            {paths.map((p) => (
              <li key={p} className="chosen">
                <strong>{fileName(p)}</strong>
                <br />
                <span className="muted">{p}</span>{" "}
                <button
                  type="button"
                  className="link"
                  onClick={() => setPaths((c) => c.filter((x) => x !== p))}
                >
                  {t("import.removeFile")}
                </button>
              </li>
            ))}
          </ul>
        )}
        <button type="button" onClick={() => void pick()} disabled={picking}>
          {picking
            ? t("import.picking")
            : t(paths.length === 0 ? "import.choose" : "import.chooseMore")}
        </button>
      </div>

      <fieldset className="stages">
        <legend>{t("import.stages")}</legend>
        {(
          [
            ["correct", "import.stageCorrect"],
            ["embed", "import.stageEmbed"],
            ["extractSemantics", "import.stageSemantics"],
            ["generateEvalset", "import.stageEvalset"],
            ["tune", "import.stageTune"],
            ["learnProfile", "import.stageLearnProfile"],
            ["reviewCorrection", "import.stageReview"],
          ] as const
        ).map(([key, label]) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={stages[key]}
              onChange={() => toggle(key)}
            />
            <span>{t(label)}</span>
          </label>
        ))}
      </fieldset>

      <button
        type="button"
        onClick={start}
        disabled={busy || paths.length === 0 || !libraryId}
      >
        {busy
          ? t("import.working")
          : t("import.startBatch", { count: paths.length })}
      </button>

      {refused.length > 0 && (
        <div className="error">
          <strong>{t("import.someRefused", { count: refused.length })}</strong>
          <ul>
            {refused.map((f) => (
              <li key={f.path}>
                <strong>{fileName(f.path)}</strong>
                <pre className="detail">{f.message}</pre>
              </li>
            ))}
          </ul>
          <button type="button" onClick={() => setRefused([])}>
            {t("error.dismiss")}
          </button>
        </div>
      )}

      {error !== null && (
        <div className="error">
          <strong>{t("error.title")}</strong>
          {errorGuidanceKey(error) && <p>{t(errorGuidanceKey(error)!)}</p>}
          <pre className="detail">{errorMessage(error)}</pre>
          <button type="button" onClick={() => setError(null)}>
            {t("error.dismiss")}
          </button>
        </div>
      )}

      {/* The queue, and the reason this screen is no longer amnesiac: it is read
          from the catalog, so it survives a relaunch, a plane switch and a
          crash, and an approved import stays on screen instead of vanishing
          behind a one-line notice. */}
      <ImportQueue
        items={items}
        loaded={loaded}
        error={queueError}
        stages={stages}
        onDecide={(id, approved, options) => void decide(id, approved, options)}
        onChanged={() => void refresh()}
        locale={i18n.language}
      />
    </section>
  );
}
