/**
 * The approval gate: what this run would cost, and the decision it is waiting on.
 *
 * Moved out of `ImportScreen` unchanged when the screen became a queue. It was
 * one screen holding one run, so the gate could be inline; a queue can hold
 * several runs parked at their own gates for up to seven days each, and each one
 * needs its own profile choice. Nothing about what it renders changed — this is
 * where the estimate table, the profile block and the two buttons already were.
 *
 * `profileChoice` lives here rather than above, and that is the substantive
 * part of the move: it is a decision about *this* report, and hoisting it would
 * let approving one item silently carry the rules chosen for another.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  Estimate,
  GateReport,
  StageEstimate,
  StageOptions,
} from "./lib/api";
import { Cost, range } from "./Money";

/** The three mutually exclusive things a person can decide about rules. */
export type ProfileChoice = "inherited" | "learn" | "defaults";

/** How each choice reaches the workflow.
 *
 *  Three states rather than two booleans, because they are mutually exclusive
 *  and a UI offering "learn" and "ignore" as independent checkboxes invites
 *  setting both — which is a request the workflow cannot honour. */
export const PROFILE_SWITCHES: Record<
  ProfileChoice,
  Pick<StageOptions, "learnProfile" | "ignoreProfile">
> = {
  inherited: { learnProfile: false, ignoreProfile: false },
  learn: { learnProfile: true, ignoreProfile: false },
  defaults: { learnProfile: false, ignoreProfile: true },
};

/** Which rules a report arrives already recommending.
 *
 *  Reuse is free, so it is the default whenever a profile exists: offering
 *  "learn again" first would quote the user for work the family has already
 *  paid for. */
export const initialChoice = (report: GateReport): ProfileChoice =>
  report.profile?.source === "reused" ? "inherited" : "learn";

export function GateReview({
  report,
  stages,
  busy,
  onDecide,
}: {
  report: GateReport;
  stages: StageOptions;
  busy: boolean;
  onDecide: (approved: boolean, options: StageOptions) => void;
}) {
  const { t } = useTranslation();
  const [profileChoice, setProfileChoice] = useState<ProfileChoice>(
    initialChoice(report),
  );
  const decide = (approved: boolean) =>
    onDecide(approved, { ...stages, ...PROFILE_SWITCHES[profileChoice] });

  return (
    <div className="gate">
      <h3>{t("gate.title")}</h3>
      <p className="intro">{t("gate.intro")}</p>

      <dl className="preview">
        <dt>{t("gate.chunks")}</dt>
        <dd>{report.preview.chunkCount}</dd>
        <dt>{t("gate.characters")}</dt>
        <dd>{report.preview.characters.toLocaleString()}</dd>
        <dt>{t("gate.kinds")}</dt>
        <dd>
          {report.preview.kinds
            .map((k) => `${k.kind} ${k.count}`)
            .join(" · ") || "—"}
        </dd>
      </dl>

      {/* Not a footnote. Correction runs before chunking because it changes
          the text's length, so with it on these are not the chunks that
          will be indexed. */}
      {!report.preview.chunksAreFinal && (
        <p className="warn">{t("gate.notFinal")}</p>
      )}
      {report.preview.warnings.map((w) => (
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
        {report.profile?.source === "reused" ? (
          <>
            <p>
              {t("gate.profileReused", {
                slug: report.profile.slug,
                learnedFrom: report.profile.learnedFrom || "—",
                revisions: report.profile.revisions,
              })}
            </p>
            {report.profile.rules.headingL1Pattern && (
              <p className="model">
                {t("gate.profileHeadings", {
                  pattern: report.profile.rules.headingL1Pattern,
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

        {report.profileWarnings.length > 0 && (
          <div className="warn">
            <strong>{t("gate.profileCollision")}</strong>
            {report.profileWarnings.map((w) => (
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
                  value === "inherited" && report.profile?.source !== "reused"
                }
                onChange={() => setProfileChoice(value)}
              />
              <span>{t(label)}</span>
            </label>
          ))}
        </fieldset>
      </div>

      <EstimateTable estimate={report.estimate} />

      <p className="caveat">{report.estimate.priceSource}</p>

      <div className="actions">
        <button type="button" onClick={() => decide(true)} disabled={busy}>
          {t("gate.approve")}
        </button>
        <button type="button" onClick={() => decide(false)} disabled={busy}>
          {t("gate.reject")}
        </button>
      </div>
    </div>
  );
}

/**
 * What a run will cost, stage by stage.
 *
 * Exported because two gates render it and a second copy would eventually quote
 * two different bills for one pipeline — the same reason `estimate_for` was
 * pulled out of `estimate_cost` on the worker side.
 *
 * A row with no tokens at all is rendered as "—" rather than as two zeroes.
 * Amazon Transcribe bills seconds of audio, so zero tokens is *true* there and
 * a pair of zeroes would read as a stage that is about to do nothing.
 */
export function EstimateTable({ estimate }: { estimate: Estimate }) {
  const { t } = useTranslation();
  const untokenised = (s: StageEstimate) =>
    s.inputTokens === 0 && s.outputTokens === 0;

  return (
    <>
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
        {estimate.stages.map((s) => (
          <tr key={s.stage}>
            <td>
              {t(`gate.stages.${s.stage}`, { defaultValue: s.stage })}
            </td>
            <td className="model">{s.model}</td>
            <td>{untokenised(s) ? "—" : s.inputTokens.toLocaleString()}</td>
            <td>
              {untokenised(s) ? (
                "—"
              ) : s.outputTokensHigh > s.outputTokens ? (
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
              usd={estimate.totalUsd}
              high={estimate.totalUsdHigh}
            />
          </td>
        </tr>
      </tfoot>
    </table>

    {/* The caveat travels with the figure, always: the token counts are
        measured, the prices are second-hand. */}

      {/* The caveat travels with the figure, always: the token counts are
          measured, the prices are second-hand. */}
      <p className="caveat">{estimate.priceSource}</p>
    </>
  );
}
