/**
 * Editing how this organisation words an answer, per effort level.
 *
 * On the Services screen rather than beside the level selector on Ask, because
 * it is an organisation-wide setting: everyone asking against this corpus gets
 * the wording saved here, so it belongs with the other things configured once
 * rather than with the control a person moves per question.
 *
 * Only the *style* is editable. The six answering rules — no general knowledge,
 * cite by chunk id, do not fill gaps — are not reachable from here and win any
 * disagreement with what is typed, which is what keeps citation verification
 * meaning something. An edit that could delete "do not use general knowledge"
 * would produce a fuller, worse-grounded answer, and that is the failure that
 * looks exactly like success.
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorGuidanceKey, errorMessage, type AnswerStyle } from "../lib/api";
import type { AskEffort } from "../lib/askEffort";

export function AnswerStyles() {
  const { t } = useTranslation();
  const [levels, setLevels] = useState<AnswerStyle[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [maxChars, setMaxChars] = useState(2000);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const styles = await api.answerStyles();
      setLevels(styles.levels);
      setMaxChars(styles.maxChars);
      // Drafts are seeded from what came back and then owned by the textarea.
      // Reseeding them on every render would fight the person typing.
      setDrafts(Object.fromEntries(styles.levels.map((l) => [l.effort, l.body])));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = useCallback(
    async (effort: AskEffort, body: string) => {
      setBusy(effort);
      try {
        await api.setAnswerStyle(effort, body);
        // Reloaded rather than patched locally: clearing an override makes the
        // server fall back to a default this screen would otherwise have to
        // know how to reproduce, and two places deciding what the default is
        // is how they come to disagree.
        await load();
      } catch (e) {
        setError(e);
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  if (error !== null) {
    const guidance = errorGuidanceKey(error);
    // A notice, deliberately not the red error panel. This is one block on a
    // screen that is mostly about something else, and it is the *first* thing
    // on it that needs the control API — so in cloud mode with no local stack,
    // or before the stack is up, it is the block most likely to fail while
    // everything around it is fine. Rendering the screen's error treatment for
    // it said the whole screen was broken, and a test that had nothing to do
    // with this feature is what noticed.
    return (
      <>
        <h3>{t("styles.title")}</h3>
        <p className="notice">
          {t("styles.unavailable")} {guidance ? t(guidance) : errorMessage(error)}
        </p>
        <button type="button" onClick={() => void load()}>
          {t("styles.retry")}
        </button>
      </>
    );
  }

  return (
    <>
      <h3>{t("styles.title")}</h3>
      <p>{t("styles.intro")}</p>
      {levels === null ? (
        <p className="notice">{t("styles.loading")}</p>
      ) : (
        levels.map((level) => {
          const draft = drafts[level.effort] ?? "";
          const dirty = draft !== level.body;
          return (
            <div className="answer-style" key={level.effort}>
              <label className="field">
                <span>
                  {t(`ask.effort.${level.effort}`)}
                  {level.custom && (
                    <span className="muted small"> · {t("styles.custom")}</span>
                  )}
                </span>
                <textarea
                  rows={4}
                  value={draft}
                  maxLength={maxChars}
                  disabled={busy !== null}
                  onChange={(e) =>
                    setDrafts((d) => ({ ...d, [level.effort]: e.target.value }))
                  }
                />
              </label>
              <div className="actions">
                <button
                  type="button"
                  disabled={!dirty || busy !== null}
                  onClick={() => void save(level.effort, draft)}
                >
                  {t("styles.save")}
                </button>
                <button
                  type="button"
                  className="link"
                  // Only where it would do something: restoring a level nobody
                  // overrode is a request that changes nothing, and offering it
                  // suggests the default is something other than what is shown.
                  disabled={!level.custom || busy !== null}
                  onClick={() => void save(level.effort, "")}
                >
                  {t("styles.restore")}
                </button>
              </div>
            </div>
          );
        })
      )}
    </>
  );
}
