import { useTranslation } from "react-i18next";

import { Locator } from "../Locator";
import type { Synthesis } from "../lib/api";

/**
 * A comparative reading of a channel, in the five sections it was asked for.
 *
 * The layout is the argument. `hallazgos` come first with their citations
 * attached, because they are the part a reader may rely on; `interpretacion`
 * sits under its own heading with each inference's scope and limits beside it,
 * because it is the part they may not; and `limitaciones` is last and is not a
 * footnote — it holds the findings that were **demoted** for naming no citation
 * the code could verify, which is the one thing a reader has to see before
 * deciding how much of the rest to believe.
 *
 * A refusal renders as a refusal, with its reason. "No answer" with nothing to
 * look at is indistinguishable from a broken index, and `off_corpus` and
 * `insufficient_evidence` ask for different things.
 */
export function ChannelSynthesis({ result }: { result: Synthesis }) {
  const { t } = useTranslation();
  const byId = new Map(result.citas.map((c) => [c.chunkId, c]));

  if (result.state !== "answered") {
    return (
      <div className="panel">
        <h3>{t("channel.synthesis")}</h3>
        <p className="warn">
          {t(`channel.state.${result.state}`, { defaultValue: result.state })}
        </p>
        {/* The reason is the only thing that says *which* refusal this was —
            four facts with four remedies used to render as one line. */}
        {result.reason && <p className="caveat">{result.reason}</p>}
      </div>
    );
  }

  return (
    <div className="panel synthesis">
      <h3>{t("channel.synthesis")}</h3>

      <h4>{t("channel.findings")}</h4>
      <ol className="findings">
        {result.hallazgos.map((finding, i) => (
          <li key={`${i}:${finding.afirmacion}`}>
            <p>{finding.afirmacion}</p>
            <ul className="citations">
              {finding.chunkIds.map((id) => {
                const cita = byId.get(id);
                if (!cita) return null;
                return (
                  <li key={id}>
                    <Locator text={cita.locator} />
                    {cita.claim && <q>{cita.claim}</q>}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ol>

      {(result.comparacion.convergencias.length > 0 ||
        result.comparacion.diferencias.length > 0 ||
        result.comparacion.matices.length > 0) && (
        <>
          <h4>{t("channel.comparison")}</h4>
          {(
            [
              ["convergencias", result.comparacion.convergencias],
              ["diferencias", result.comparacion.diferencias],
              ["matices", result.comparacion.matices],
            ] as const
          ).map(([key, items]) =>
            items.length === 0 ? null : (
              <div key={key}>
                <h5>{t(`channel.comparison_.${key}`)}</h5>
                <ul>
                  {items.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ),
          )}
        </>
      )}

      {result.interpretacionTeologica.length > 0 && (
        <>
          <h4>{t("channel.interpretation")}</h4>
          {/* Its own heading and its own caveat, always. This is the part that
              is easiest to mistake for evidence and the part with none. */}
          <p className="caveat">{t("channel.interpretationCaveat")}</p>
          <ul className="inferences">
            {result.interpretacionTeologica.map((item) => (
              <li key={item.inferencia}>
                <p>{item.inferencia}</p>
                <p className="candidate-meta">
                  {t("channel.scope", { scope: item.alcance })} ·{" "}
                  {t("channel.limits", { limits: item.limites })}
                </p>
              </li>
            ))}
          </ul>
        </>
      )}

      {result.limitaciones.length > 0 && (
        <>
          <h4>{t("channel.limits_")}</h4>
          <ul>
            {result.limitaciones.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </>
      )}

      {/* Not a footnote either. A synthesis where half the findings were
          demoted is one to distrust, and a count is the only thing that says
          so; an invented chunk id is the model citing something it was never
          shown, which is the most dangerous failure this product can have. */}
      {(result.demoted > 0 || result.invented > 0) && (
        <p className="warn">
          {result.demoted > 0 && t("channel.demoted", { count: result.demoted })}
          {result.demoted > 0 && result.invented > 0 ? " " : ""}
          {result.invented > 0 && t("channel.invented", { count: result.invented })}
        </p>
      )}
    </div>
  );
}
