import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorMessage,
  type DocumentRow,
  type RunSummary,
  type TransformGateReport,
  type TransformOptions,
  type TransformPlanReport,
  type TransformVocabulary,
} from "../lib/api";
import { useLibraries } from "../lib/libraries";
import {
  TransformPlanReview,
  TransformQuote,
} from "../TransformGateReview";

/**
 * Recasting a document in this library into another literary genre.
 *
 * **Paid plane only.** The local plane serves none of these routes, by decision
 * — recorded in `doc/TRANSFORM.md` — so in local mode every call here comes back
 * as the 404 the proxy turns into `control_status`, and the screen renders that
 * rather than pretending. The count at the top of the root `CLAUDE.md` is why
 * that sentence is written out: "paid plane" says which of two backends serves
 * it and nothing about how many clients exist.
 *
 * **The two gates live here rather than in the import queue**, and that is a
 * trade-off worth naming. A transform run carries a `library_id`, so its *row*
 * appears in the queue for free, which is the visibility the channel feature was
 * reported for lacking. What the queue cannot carry is the panels: its
 * `onDecide` is typed on `StageOptions`, and a transformation's two switches are
 * not stage switches. So the queue shows that a run is waiting and this screen
 * is where it is answered.
 */
export function TransformScreen({ active }: { active: boolean }) {
  const { t } = useTranslation();
  const { selected } = useLibraries();

  const [vocabulary, setVocabulary] = useState<TransformVocabulary | null>(null);
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [documentId, setDocumentId] = useState("");
  const [genre, setGenre] = useState("");
  const [mode, setMode] = useState("");
  const [purposes, setPurposes] = useState<string[]>([]);
  const [reviewPlan, setReviewPlan] = useState(true);

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [gates, setGates] = useState<Record<string, TransformGateReport>>({});
  const [plans, setPlans] = useState<Record<string, TransformPlanReport>>({});
  const [busy, setBusy] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  /** Only a document with an indexed version can be recast: the work is made
   *  from that version's `chunks.jsonl`, which is the only artifact carrying
   *  the outline the chunker detected. */
  const recastable = useMemo(
    () => documents.filter((d) => d.activeVersionId !== null),
    [documents],
  );
  const chosen = recastable.find((d) => d.id === documentId) ?? null;

  useEffect(() => {
    if (!active || vocabulary !== null) return;
    let alive = true;
    void api
      .genres()
      .then((v) => {
        if (!alive) return;
        setVocabulary(v);
        setGenre((g) => g || v.genres[0] || "");
        setMode((m) => m || v.defaultMode);
      })
      .catch((e) => alive && setError(errorMessage(e)));
    return () => {
      alive = false;
    };
  }, [active, vocabulary]);

  useEffect(() => {
    if (!active || !selected) return;
    let alive = true;
    void api
      .libraryDocuments(selected, false)
      .then((l) => alive && setDocuments(l.documents))
      .catch((e) => alive && setError(errorMessage(e)));
    return () => {
      alive = false;
    };
  }, [active, selected]);

  /** This library's transformations, and whichever gate each is parked at.
   *
   *  Polled while the screen is open rather than subscribed to: a gate is
   *  answered by a person over minutes, and the two queries are cheap reads
   *  against Temporal. A 409 means "not there yet", which is the ordinary first
   *  answer and is why `transformGate` resolves to null rather than throwing.
   */
  const refresh = useCallback(async () => {
    if (!selected) return;
    try {
      // Narrowed in the **request**, never over what arrived. The queue's own
      // limit is 30, so filtering afterwards would show whichever of the last
      // 30 runs of every kind happened to be transformations — a different and
      // much shorter list than "the last 30 transformations", which is the
      // recorded reason the import queue takes its library the same way.
      const page = await api.runsList({
        libraryId: selected,
        kinds: "transform",
        limit: 30,
      });
      const mine = page.runs;
      setRuns(mine);
      for (const run of mine) {
        if (run.state !== "awaiting_approval") continue;
        try {
          const [gate, plan] = await Promise.all([
            api.transformGate(run.workflowId),
            api.transformPlan(run.workflowId),
          ]);
          if (gate) setGates((g) => ({ ...g, [run.workflowId]: gate }));
          if (plan) setPlans((p) => ({ ...p, [run.workflowId]: plan }));
        } catch {
          // A run whose history has aged out, or a plane that is down. Neither
          // is something this screen can act on, and neither is a reason to
          // paint an error over a list that is otherwise correct.
        }
      }
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [selected]);

  useEffect(() => {
    if (!active) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 4000);
    return () => clearInterval(timer);
  }, [active, refresh]);

  const start = useCallback(async () => {
    if (!selected || !chosen?.activeVersionId || !genre) return;
    setBusy(true);
    setError(null);
    try {
      await api.startTransform(
        {
          libraryId: selected,
          documentId: chosen.id,
          versionId: chosen.activeVersionId,
          genre,
          mode,
          purposes,
          label: chosen.title,
        },
        { reviewPlan, research: purposes.length > 0 },
      );
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [chosen, genre, mode, purposes, reviewPlan, refresh, selected]);

  const decide = useCallback(
    async (workflowId: string, approved: boolean, options: TransformOptions) => {
      setDeciding(true);
      try {
        await api.approveTransform(workflowId, { approved, options });
        // Dropped so the next poll fetches whichever gate comes next rather
        // than re-rendering the one just answered.
        setGates((g) => {
          const { [workflowId]: _gone, ...rest } = g;
          return rest;
        });
        setPlans((p) => {
          const { [workflowId]: _gone, ...rest } = p;
          return rest;
        });
        await refresh();
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        // **`finally`, not the happy path.** A refused approval that left this
        // latched would disable both buttons with the panel still on screen and
        // no way to retry — the recorded defect one screen over, where the only
        // remedy was relaunching the app.
        setDeciding(false);
      }
    },
    [refresh],
  );

  const download = useCallback(
    async (workflowId: string, title: string) => {
      setBusy(true);
      try {
        const path = await api.artifactSave(
          workflowId,
          "transform",
          `${title || "obra"}.md`,
        );
        setSaved(path);
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const toggle = (purpose: string) =>
    setPurposes((current) =>
      current.includes(purpose)
        ? current.filter((p) => p !== purpose)
        : [...current, purpose],
    );

  return (
    <section className="screen transform-screen">
      <h2>{t("transform.title")}</h2>
      <p className="muted">{t("transform.intro")}</p>

      {error && <p className="error">{error}</p>}
      {saved && <p className="muted small">{t("transform.saved", { path: saved })}</p>}

      <div className="transform-form">
        <label>
          {t("transform.document")}
          <select
            value={documentId}
            onChange={(e) => setDocumentId(e.target.value)}
          >
            <option value="">{t("transform.pickDocument")}</option>
            {recastable.map((d) => (
              <option key={d.id} value={d.id}>
                {d.title}
              </option>
            ))}
          </select>
        </label>

        <label>
          {t("transform.genreLabel")}
          <select value={genre} onChange={(e) => setGenre(e.target.value)}>
            {(vocabulary?.genres ?? []).map((g) => (
              <option key={g} value={g}>
                {t(`transform.genre.${g}`, { defaultValue: g })}
              </option>
            ))}
          </select>
        </label>

        <fieldset className="transform-modes">
          <legend>{t("transform.modeLabel")}</legend>
          {(vocabulary?.modes ?? []).map((m) => (
            <label key={m}>
              <input
                type="radio"
                name="transform-mode"
                value={m}
                checked={mode === m}
                onChange={() => setMode(m)}
              />
              {t(`transform.mode.${m}`, { defaultValue: m })}
              <span className="muted small">
                {" "}
                {t(`transform.modeHint.${m}`, { defaultValue: "" })}
              </span>
            </label>
          ))}
        </fieldset>

        <fieldset className="transform-purposes">
          <legend>{t("transform.purposes")}</legend>
          <p className="muted small">{t("transform.purposesHint")}</p>
          {(vocabulary?.purposes ?? []).map((p) => (
            <label key={p}>
              <input
                type="checkbox"
                checked={purposes.includes(p)}
                onChange={() => toggle(p)}
              />
              {t(`transform.purpose.${p}`, { defaultValue: p })}
            </label>
          ))}
        </fieldset>

        <label>
          <input
            type="checkbox"
            checked={reviewPlan}
            onChange={(e) => setReviewPlan(e.target.checked)}
          />
          {t("transform.option.reviewPlan")}
        </label>

        <button type="button" disabled={busy || !chosen || !genre} onClick={() => void start()}>
          {t("transform.start")}
        </button>
      </div>

      <h3>{t("transform.runs")}</h3>
      {runs.length === 0 ? (
        <p className="muted">{t("transform.noRuns")}</p>
      ) : (
        <ul className="transform-runs">
          {runs.map((run) => (
            <li key={run.workflowId}>
              <div className="row">
                <strong>{run.title ?? run.workflowId}</strong>
                <span className="muted small">
                  {t(`run.state.${run.state}`, { defaultValue: run.state })}
                </span>
                {run.state === "succeeded" && (
                  <button
                    type="button"
                    className="link"
                    disabled={busy}
                    onClick={() =>
                      void download(run.workflowId, run.title ?? "obra")
                    }
                  >
                    {t("transform.download")}
                  </button>
                )}
              </div>

              {/* The plan report first: a run parked at the *second* gate has
                  both, and showing the first one's quote there would be exactly
                  the defect this feature was built around. */}
              {plans[run.workflowId] !== undefined ? (
                <TransformPlanReview
                  report={plans[run.workflowId]!}
                  options={{ reviewPlan, research: purposes.length > 0 }}
                  busy={deciding}
                  onDecide={(approved, options) =>
                    void decide(run.workflowId, approved, options)
                  }
                />
              ) : gates[run.workflowId] !== undefined ? (
                <TransformQuote
                  report={gates[run.workflowId]!}
                  options={{ reviewPlan, research: purposes.length > 0 }}
                  busy={deciding}
                  onDecide={(approved, options) =>
                    void decide(run.workflowId, approved, options)
                  }
                />
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
