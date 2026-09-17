import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { MediaLocator } from "../MediaLocator";
import { Narrowings, activeNarrowings } from "../Narrowings";
import { bucketIdFor } from "../lib/bucket";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type AnswerState,
  type AskNarrowings,
  type EvidenceItem,
} from "../lib/api";
import {
  ASK_EFFORTS,
  loadEffort,
  saveEffort,
  type AskEffort,
} from "../lib/askEffort";
import {
  askReducer,
  citedEvidence,
  lastId,
  loadSession,
  saveSession,
  selectedEntry,
} from "../lib/askSession";
import { useBackend } from "../lib/backend";
import { useLibraries } from "../lib/libraries";

/**
 * The three outcomes are rendered differently on purpose.
 *
 * `answered` carries citations the backend verified. `insufficient_evidence`
 * means the corpus was searched and came up short. `off_corpus` means nothing
 * cleared the similarity floor at all. They have three different fixes, and
 * collapsing them into "no results" sends the user to the wrong one.
 */
const STATE_KEY: Record<AnswerState, string> = {
  answered: "ask.answered",
  insufficient_evidence: "ask.insufficient",
  off_corpus: "ask.offCorpus",
};

/**
 * What the right-hand column says when there is nothing to cite.
 *
 * Both refusals still carry passages — `off_corpus` the ones that came nearest
 * without clearing the floor, `insufficient_evidence` the ones that were
 * retrieved and did not support an answer. Those passages are the diagnostic:
 * they are how you tell "wrong library" from "wrong question", and how you see
 * what the corpus does have. Replacing them with an empty state throws away
 * work that was already paid for.
 */
const NOT_CITED_KEY: Record<AnswerState, string> = {
  answered: "ask.noEvidence",
  insufficient_evidence: "ask.notCitedInsufficient",
  off_corpus: "ask.notCitedOffCorpus",
};

/** One retrieved passage, rendered whole. This panel exists to be read against
 *  a claim, so it is not the truncated preview the old `<details>` list was.
 *
 *  Exported because the Chat screen renders the same thing, and the same
 *  reasoning `DocumentGraph`'s `Swatch` records applies: one component means one
 *  stylesheet rule moves both, rather than two that drift until somebody
 *  notices the passages look different on two screens. */
export function Passage({ item, libraryId }: { item: EvidenceItem; libraryId?: string | null }) {
  const { t } = useTranslation();
  return (
    <div className="passage">
      <span className="title">{item.title}</span>
      {item.breadcrumb && <span className="breadcrumb">{item.breadcrumb}</span>}
      <span className="source">
        {t(`ask.source.${item.source}`, { defaultValue: item.source })}
      </span>
      <p className="chunk-text">{item.text}</p>
      <MediaLocator locator={item.locator} libraryId={libraryId ?? null} documentId={item.documentId} />
    </div>
  );
}

/** How often to collect a question in flight. Answering takes tens of seconds
 *  to minutes, so this is about how soon the answer appears once it exists, not
 *  about how hard the API is worked. */
const POLL_MS = 1500;

export function AskScreen() {
  const { t } = useTranslation();

  const { selected: libraryId } = useLibraries();
  const [text, setText] = useState("");
  // The screen is remounted when the plane changes (see `App`), so the identity
  // read here is always the one whose history belongs on screen, and the lazy
  // initialiser runs again rather than carrying the other plane's questions.
  const { identity } = useBackend();
  const [session, dispatch] = useReducer(askReducer, identity, loadSession);
  // Same lazy-initialiser-on-identity shape as the history above, and for the
  // same reason: the remount on a plane change re-reads the level belonging to
  // the plane now on screen.
  const [effort, setEffort] = useState<AskEffort>(() => loadEffort(identity));
  // Not persisted: a filter that outlived the session that set it would be a
  // silent one, and the screen it belongs to is remounted on a plane change.
  const [narrowings, setNarrowings] = useState<AskNarrowings>({});
  const isBucket = bucketIdFor(libraryId) !== null;
  const nextId = useRef(lastId(session));
  const [now, setNow] = useState(() => Date.now());

  const entry = selectedEntry(session);
  const answer = entry?.answer ?? null;
  // One question at a time. Letting a second run concurrently would mean
  // concurrent unbudgeted spend.
  const busy = session.entries.some((e) => e.status === "pending");

  useEffect(() => {
    saveSession(session, identity);
  }, [session, identity]);

  useEffect(() => {
    saveEffort(effort, identity);
  }, [effort, identity]);

  /** The questions in flight, as a string so the effects below re-run when the
   *  set changes rather than on every unrelated dispatch. */
  const collecting = session.entries
    .filter((e) => e.status === "pending" && e.questionId)
    .map((e) => `${e.id}:${e.questionId}`)
    .join(",");

  // Collect. The first pass runs immediately rather than after POLL_MS, which
  // is what makes a relaunch pick up an answer computed while the window was
  // closed without a visible pause.
  useEffect(() => {
    if (!collecting) return;
    let stopped = false;

    const collect = async () => {
      for (const pair of collecting.split(",")) {
        const [id, questionId] = pair.split(":");
        if (!id || !questionId) continue;
        try {
          const progress = await api.askResult(questionId);
          if (stopped) return;
          if (progress.state === "done" && progress.answer) {
            dispatch({ type: "answered", id, answer: progress.answer });
          } else if (progress.state === "failed") {
            dispatch({ type: "failed", id, error: progress.error });
          }
        } catch (e) {
          if (!stopped) dispatch({ type: "failed", id, error: e });
        }
      }
    };

    void collect();
    const timer = setInterval(() => void collect(), POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [collecting]);

  // A silent spinner is indistinguishable from a hang, and that is exactly what
  // was reported. The clock runs while anything is pending — `busy`, not
  // `collecting`: a question is pending from the moment it is asked, and only
  // gets an id once the API has accepted it. Keying this to `collecting` froze
  // the display at 0s for the whole handover, which is the part that is slow
  // when something is wrong with it.
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  const elapsed = (from: number) => Math.max(0, Math.round((now - from) / 1000));

  /** Hand a question over and record the id it comes back with. Shared by the
   *  composer and by "ask again", which is the only way back to a question the
   *  composer already cleared. */
  //
  // The level is a parameter rather than read from state here, because the two
  // callers mean different things by it: the composer asks at the level the
  // control currently shows, while "ask again" asks at the level that entry was
  // originally asked at. Closing over the state would have silently re-asked
  // every old question at whatever the control happens to say now, which is the
  // one thing that would make two entries incomparable.
  const startAsk = useCallback(
    async (
      question: string,
      library: string,
      level: AskEffort,
      narrow: AskNarrowings = {},
    ) => {
      const id = `q${(nextId.current += 1)}`;
      dispatch({
        type: "submit",
        id,
        question,
        libraryId: library,
        startedAt: Date.now(),
        effort: level,
      });
      try {
        const { questionId } = await api.ask({
          library_id: library,
          text: question,
          effort: level,
          ...activeNarrowings(narrow),
        });
        dispatch({ type: "started", id, questionId });
      } catch (e) {
        dispatch({ type: "failed", id, error: e });
      }
    },
    [],
  );

  const submit = useCallback(() => {
    const question = text.trim();
    if (!question || !libraryId) return;
    setText("");
    void startAsk(question, libraryId, effort, isBucket ? narrowings : {});
  }, [libraryId, text, startAsk, effort, narrowings, isBucket]);

  const spent = answer?.spend.reduce((sum, s) => sum + (s.usd ?? 0), 0) ?? 0;
  const anyUnpriced = answer?.spend.some((s) => s.usd === null) ?? false;
  const cited = citedEvidence(entry);
  const guidance = entry?.error ? errorGuidanceKey(entry.error) : undefined;

  return (
    <section className="screen ask">
      <h2>{t("ask.title")}</h2>
      <p className="intro">{t("ask.intro")}</p>

      <div className="ask-columns">
        <div className="pane pane-history">
          <h3>{t("ask.history")}</h3>
          {session.entries.length === 0 ? (
            <p className="notice">{t("ask.historyEmpty")}</p>
          ) : (
            <ul className="history">
              {session.entries.map((e) => (
                <li key={e.id}>
                  <button
                    type="button"
                    className={`link${e.id === session.selected ? " active" : ""}`}
                    onClick={() => dispatch({ type: "select", id: e.id })}
                  >
                    {e.question}
                  </button>
                  <span className="muted small">
                    {t("ask.askedOf", { id: e.libraryId })}
                    {e.status === "pending" &&
                      ` · ${t("ask.thinkingFor", { seconds: elapsed(e.startedAt) })}`}
                    {e.status === "failed" && ` · ${t("ask.failed")}`}
                  </span>
                  {e.status !== "pending" && (
                    <button
                      type="button"
                      className="link small"
                      disabled={busy || !libraryId}
                      onClick={() => void startAsk(e.question, libraryId, e.effort)}
                    >
                      {t("ask.again")}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="pane pane-answer">
          {entry === null ? (
            <p className="notice">{t("ask.nothingSelected")}</p>
          ) : (
            <>
              <p className="asked">{entry.question}</p>

              {entry.status === "pending" && (
                <p className="progress">
                  {t("ask.thinkingFor", { seconds: elapsed(entry.startedAt) })}
                  {" · "}
                  {t("ask.keepsRunning")}
                </p>
              )}

              {entry.error !== null && (
                <div className="error">
                  <strong>{t("error.title")}</strong>
                  {guidance && <p>{t(guidance)}</p>}
                  <pre className="detail">{errorMessage(entry.error)}</pre>
                </div>
              )}

              {answer && (
                <div className={`answer ${answer.state}`}>
                  <strong>{t(STATE_KEY[answer.state])}</strong>

                  {answer.state === "answered" ? (
                    <p className="prose">{answer.text}</p>
                  ) : (
                    <p className="reason">{answer.reason}</p>
                  )}

                  <p className="caveat">
                    {anyUnpriced
                      ? t("ask.spentUnpriced")
                      : t("ask.spent", { usd: spent.toFixed(6) })}
                  </p>
                </div>
              )}
            </>
          )}

          {/* The composer sits below the answer rather than above it: the
              question you are about to ask is usually the one the answer you
              just read provoked. Selecting a history entry deliberately does
              not refill it — that would throw away whatever you had typed. */}
          <label className="field">
            <span>{t("ask.question")}</span>
            <textarea
              rows={3}
              value={text}
              placeholder={t("ask.placeholder")}
              onChange={(e) => setText(e.target.value)}
            />
          </label>

          {/* Below the question rather than above it, so the label "Question"
              stays next to its field. Three radios rather than three
              checkboxes: the levels are mutually exclusive, and the same
              reasoning the import gate records about its profile choice
              applies — independent switches would invite a request nobody can
              honour. `fieldset.stages` is reused verbatim, so this control has
              no layout of its own to break at a narrow width. */}
          <fieldset className="stages">
            <legend>{t("ask.effortLegend")}</legend>
            {ASK_EFFORTS.map((level) => (
              <label key={level}>
                <input
                  type="radio"
                  name="ask-effort"
                  value={level}
                  checked={effort === level}
                  disabled={busy}
                  onChange={() => setEffort(level)}
                />
                <span>{t(`ask.effort.${level}`)}</span>
                <span className="muted small">{t(`ask.effortHint.${level}`)}</span>
              </label>
            ))}
          </fieldset>

          {isBucket && (
            <Narrowings value={narrowings} onChange={setNarrowings} disabled={busy} />
          )}

          <button
            type="button"
            onClick={submit}
            disabled={busy || !text.trim() || !libraryId}
          >
            {busy ? t("ask.thinking") : t("ask.submit")}
          </button>
        </div>

        <div className="pane pane-citations">
          {answer === null ? (
            <p className="notice">{t("ask.noAnswerYet")}</p>
          ) : answer.state === "answered" ? (
            <>
              <h3>{t("ask.citations")}</h3>
              <ol className="citations">
                {answer.citations.map((c, i) => (
                  <li key={`${c.chunkId}:${c.claim}`}>
                    <button
                      type="button"
                      className={`link${i === entry?.citation ? " active" : ""}`}
                      onClick={() => dispatch({ type: "citation", index: i })}
                    >
                      {c.claim}
                    </button>
                    <MediaLocator
                      locator={c.locator}
                      libraryId={entry?.libraryId ?? libraryId}
                      documentId={answer.evidence.find((e) => e.chunkId === c.chunkId)?.documentId}
                    />
                  </li>
                ))}
              </ol>

              <h3>{t("ask.citedPassage")}</h3>
              {cited ? (
                <Passage item={cited} libraryId={entry?.libraryId ?? libraryId} />
              ) : (
                <p className="notice">{t("ask.noEvidence")}</p>
              )}
            </>
          ) : (
            <>
              {/* `ask.evidence` already carries the count, and the count is the
                  first thing worth knowing here: zero means nothing came near,
                  which is a different failure from six that did not answer. */}
              <h3>{t("ask.evidence", { count: answer.evidence.length })}</h3>
              <p className="notice">{t(NOT_CITED_KEY[answer.state])}</p>
              {answer.evidence.map((e) => (
                <Passage key={e.chunkId} item={e} libraryId={entry?.libraryId ?? libraryId} />
              ))}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
