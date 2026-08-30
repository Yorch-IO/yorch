import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  DEFAULT_STAGES,
  errorGuidanceKey,
  errorMessage,
  type GateReport,
  type StageOptions,
} from "../lib/api";
import { useLibraries } from "../lib/libraries";

/** How often to ask whether the free stages have finished. */
const POLL_MS = 1500;

/**
 * Money is only ever shown when it is known.
 *
 * `null` means the model has no recorded price, which the gate renders as "not
 * priced". Formatting it as $0.00 would tell the user a paid stage is free.
 */
function Money({ usd }: { usd: number | null }) {
  const { t } = useTranslation();
  if (usd === null)
    return <span className="unpriced">{t("gate.unpriced")}</span>;
  return <span>${usd.toFixed(6)}</span>;
}

/** A cost that may be a range.
 *
 *  Semantic extraction is a mean over a corpus whose documents vary by more than
 *  2x, so one number could not both cover the worst document and stay within
 *  reach of the smallest — the gate under-reported a real run by 22% because it
 *  had to pick one. Two figures let it stop picking.
 *
 *  Renders a single figure when the ends agree, which is every stage but that
 *  one, so nothing on this screen grows a range it has no measurement for. */
/** Two figures that have to read as one value.
 *
 *  The non-breaking space glues the dash to the low figure, so the only break
 *  opportunity is after the dash. Measured at 520px: without this the dash sat
 *  alone on its own line, and forcing `nowrap` instead pushed the table past the
 *  viewport with the high figure clipped. */
export function range(low: string, high: string): string {
  return `${low}\u00a0\u2013 ${high}`;
}

export function Cost({ usd, high }: { usd: number | null; high: number | null }) {
  if (usd === null || high === null || high <= usd) return <Money usd={usd} />;
  return (
    <span className="range">
      {range(`$${usd.toFixed(6)}`, `$${high.toFixed(6)}`)}
    </span>
  );
}

/** The three mutually exclusive things a person can decide about rules. */
type ProfileChoice = "inherited" | "learn" | "defaults";

/** How each choice reaches the workflow.
 *
 * `ignoreProfile` only means anything when a profile was found, and
 * `learnProfile` only when one was not — so "defaults" sets both, which is the
 * one combination that says "neither inherit nor pay to learn" whichever case
 * the run turned out to be.
 */
const PROFILE_SWITCHES: Record<
  ProfileChoice,
  Pick<StageOptions, "learnProfile" | "ignoreProfile">
> = {
  inherited: { learnProfile: false, ignoreProfile: false },
  learn: { learnProfile: true, ignoreProfile: false },
  defaults: { learnProfile: false, ignoreProfile: true },
};

export function ImportScreen() {
  const { t } = useTranslation();

  const { selected: libraryId } = useLibraries();
  const [path, setPath] = useState("");
  const [stages, setStages] = useState<StageOptions>(DEFAULT_STAGES);
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [gate, setGate] = useState<GateReport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  /** The terminal state a run ended in, when it ended without reaching its
   *  gate. Kept apart from `error` because nothing threw: the request that
   *  found out succeeded, and what it reported is a fact about the run. */
  const [runFailed, setRunFailed] = useState<string | null>(null);
  /** What the gate should do about rules. Three states rather than two booleans
   *  because they are mutually exclusive choices, and a UI offering
   *  "learn" and "ignore" as independent checkboxes invites setting both. */
  const [profileChoice, setProfileChoice] = useState<ProfileChoice>("learn");
  const timer = useRef<number | undefined>(undefined);

  const stopPolling = useCallback(() => {
    if (timer.current !== undefined) {
      window.clearInterval(timer.current);
      timer.current = undefined;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const start = useCallback(async () => {
    if (!libraryId) return;
    setBusy(true);
    setError(null);
    setGate(null);
    setOutcome(null);
    setRunFailed(null);
    try {
      // Staging is what makes one screen serve both planes. Local mode hands
      // the path straight back; cloud mode uploads the file and returns the
      // path *inside the worker's container*, which is the only path
      // `ingestStart` can use there. The key comes back too, because the two
      // planes derive it differently and the screen should not have to know.
      const staged = await api.stageSource(path);
      const run = await api.ingestStart(
        {
          libraryId,
          sourcePath: staged.sourcePath,
          sourceKey: staged.sourceKey,
          title: "",
        },
        stages,
      );
      setWorkflowId(run.workflowId);

      stopPolling();
      timer.current = window.setInterval(async () => {
        try {
          const report = await api.ingestGate(run.workflowId);
          if (!report) {
            // The gate is not ready — which is the ordinary answer for the
            // first few seconds, and is also what a run that *died* before
            // publishing one answers for as long as anybody asks. Only the run
            // state tells the two apart, so a poll that never asks spins
            // forever on a failure. Observed 2026-08-28.
            const status = await api.runStatus(run.workflowId);
            if (status.state !== null && status.state !== "running") {
              setRunFailed(status.state);
              stopPolling();
            }
            return;
          }
          if (report) {
            setGate(report);
            // Reuse is free, so it is the default whenever a profile exists.
            // Offering "learn again" first would quote the user for work the
            // family has already paid for.
            setProfileChoice(
              report.profile?.source === "reused" ? "inherited" : "learn",
            );
            stopPolling();
          }
        } catch (e) {
          setError(e);
          stopPolling();
        }
      }, POLL_MS);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }, [libraryId, path, stages, stopPolling]);

  const decide = useCallback(
    async (approved: boolean) => {
      if (!workflowId) return;
      setBusy(true);
      try {
        await api.ingestApprove(workflowId, {
          approved,
          options: { ...stages, ...PROFILE_SWITCHES[profileChoice] },
          reason: "",
        });
        setOutcome(approved ? "gate.approved" : "gate.rejected");
        setGate(null);
      } catch (e) {
        setError(e);
      } finally {
        setBusy(false);
      }
    },
    [workflowId, stages, profileChoice],
  );

  const toggle = (key: keyof StageOptions) =>
    setStages((s) => ({ ...s, [key]: !s[key] }));

  return (
    <section className="screen">
      <h2>{t("import.title")}</h2>
      <p className="intro">{t("import.intro")}</p>

      <label className="field">
        <span>{t("import.path")}</span>
        <input
          value={path}
          placeholder={t("import.pathHint")}
          onChange={(e) => setPath(e.target.value)}
        />
      </label>

      <fieldset className="stages">
        <legend>{t("import.stages")}</legend>
        {(
          [
            ["correct", "import.stageCorrect"],
            ["embed", "import.stageEmbed"],
            ["extractSemantics", "import.stageSemantics"],
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

      <button type="button" onClick={start} disabled={busy || !path.trim() || !libraryId}>
        {busy ? t("import.working") : t("import.start")}
      </button>

      {workflowId && !gate && !outcome && (
        <p className="waiting">{t("import.waiting", { id: workflowId })}</p>
      )}

      {outcome && <p className="notice">{t(outcome)}</p>}

      {gate && (
        <div className="gate">
          <h3>{t("gate.title")}</h3>
          <p className="intro">{t("gate.intro")}</p>

          <dl className="preview">
            <dt>{t("gate.chunks")}</dt>
            <dd>{gate.preview.chunkCount}</dd>
            <dt>{t("gate.characters")}</dt>
            <dd>{gate.preview.characters.toLocaleString()}</dd>
            <dt>{t("gate.kinds")}</dt>
            <dd>
              {gate.preview.kinds
                .map((k) => `${k.kind} ${k.count}`)
                .join(" · ") || "—"}
            </dd>
          </dl>

          {/* Not a footnote. Correction runs before chunking because it changes
              the text's length, so with it on these are not the chunks that
              will be indexed. */}
          {!gate.preview.chunksAreFinal && (
            <p className="warn">{t("gate.notFinal")}</p>
          )}
          {gate.preview.warnings.map((w) => (
            <p className="warn" key={w}>
              {w}
            </p>
          ))}

          {/* Which rules will chunk this document, and where they came from.
              Shown at the gate rather than buried in a run log because
              "inherited someone else's rules" and "about to pay to learn new
              ones" are different decisions, and only one of them costs money. */}
          <div className="profile">
            <h4>{t("gate.profileTitle")}</h4>
            {gate.profile?.source === "reused" ? (
              <>
                <p>
                  {t("gate.profileReused", {
                    slug: gate.profile.slug,
                    learnedFrom: gate.profile.learnedFrom || "—",
                    revisions: gate.profile.revisions,
                  })}
                </p>
                {gate.profile.rules.headingL1Pattern && (
                  <p className="model">
                    {t("gate.profileHeadings", {
                      pattern: gate.profile.rules.headingL1Pattern,
                    })}
                  </p>
                )}
              </>
            ) : (
              <>
                <p>{t("gate.profileDefault")}</p>
                <p className={stages.learnProfile ? "" : "warn"}>
                  {t(
                    stages.learnProfile
                      ? "gate.profileWillLearn"
                      : "gate.profileNoLearn",
                  )}
                </p>
              </>
            )}

            {gate.profileWarnings.length > 0 && (
              <div className="warn">
                <strong>{t("gate.profileCollision")}</strong>
                {gate.profileWarnings.map((w) => (
                  <p key={w.profileId + w.detail}>{w.detail}</p>
                ))}
                {/* A *low* topical overlap is the dangerous case: identical
                    structure, unrelated subject matter. The metrics cannot see
                    it, so the choice below is a person's to make. */}
                <p className="caveat">{t("gate.profileCollisionHelp")}</p>
              </div>
            )}

            <fieldset className="stages">
              <legend>{t("gate.profileChoice")}</legend>
              {(
                [
                  ["inherited", "gate.profileUseInherited"],
                  ["learn", "gate.profileLearnFresh"],
                  ["defaults", "gate.profileUseDefaults"],
                ] as const
              ).map(([value, label]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name="profile-choice"
                    checked={profileChoice === value}
                    disabled={
                      value === "inherited" && gate.profile?.source !== "reused"
                    }
                    onChange={() => setProfileChoice(value)}
                  />
                  <span>{t(label)}</span>
                </label>
              ))}
            </fieldset>
          </div>

          <table className="estimate">
            <thead>
              <tr>
                <th>{t("gate.stage")}</th>
                <th>{t("gate.model")}</th>
                <th>{t("gate.tokensIn")}</th>
                <th>{t("gate.tokensOut")}</th>
                <th>{t("gate.cost")}</th>
              </tr>
            </thead>
            <tbody>
              {gate.estimate.stages.map((s) => (
                <tr key={s.stage}>
                  <td>
                    {t(`gate.stages.${s.stage}`, { defaultValue: s.stage })}
                  </td>
                  <td className="model">{s.model}</td>
                  <td>{s.inputTokens.toLocaleString()}</td>
                  <td>
                    {s.outputTokensHigh > s.outputTokens ? (
                      <span className="range">
                        {range(
                          s.outputTokens.toLocaleString(),
                          s.outputTokensHigh.toLocaleString(),
                        )}
                      </span>
                    ) : (
                      s.outputTokens.toLocaleString()
                    )}
                  </td>
                  <td>
                    <Cost usd={s.usd} high={s.usdHigh} />
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={4}>{t("gate.total")}</td>
                <td>
                  <Cost
                    usd={gate.estimate.totalUsd}
                    high={gate.estimate.totalUsdHigh}
                  />
                </td>
              </tr>
            </tfoot>
          </table>

          {/* The caveat travels with the figure, always: the token counts are
              measured, the prices are second-hand. */}
          <p className="caveat">{gate.estimate.priceSource}</p>

          <div className="actions">
            <button type="button" onClick={() => decide(true)} disabled={busy}>
              {t("gate.approve")}
            </button>
            <button type="button" onClick={() => decide(false)} disabled={busy}>
              {t("gate.reject")}
            </button>
          </div>
        </div>
      )}

      {runFailed !== null && (
        <div className="error">
          <strong>{t("error.title")}</strong>
          <p>{t("import.runEnded", { state: t(`import.runState.${runFailed}`) })}</p>
          <button type="button" onClick={() => setRunFailed(null)}>
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
    </section>
  );
}
