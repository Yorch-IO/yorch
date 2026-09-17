import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Cost } from "../Money";
import {
  cost,
  duration,
  hasCaptions,
  indexed,
  outsideFilter,
  selectable,
  toggle,
  totals,
  type Row,
} from "../lib/channel";
import { KEYWORDS_SHOWN, type Keyword } from "../lib/keywords";

/**
 * The words the channel's titles and descriptions repeat, as toggles.
 *
 * Free, and computed from the catalogue already on screen — no call, no quota.
 * A pressed chip narrows the table to videos carrying the word and writes the
 * selection into the topic field, so the same word that shortlists the videos
 * is what the model is then asked to look for in them. The count beside each
 * word is how many videos carry it, which is the number a person is choosing
 * by: a word on two videos is a topic, a word on sixty is the channel.
 *
 * Folded at `KEYWORDS_SHOWN` because forty pills is a wall; the fold is a
 * button that says how many more there are rather than an ellipsis.
 */
export function KeywordChips({
  keywords,
  selected,
  onToggle,
  onClear,
}: {
  keywords: Keyword[];
  selected: ReadonlySet<string>;
  onToggle: (key: string) => void;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? keywords : keywords.slice(0, KEYWORDS_SHOWN);
  const hidden = keywords.length - shown.length;

  return (
    <div className="panel keywords">
      <h3>{t("channel.keywords")}</h3>
      <p className="caveat">{t("channel.keywordsCaveat")}</p>
      {keywords.length === 0 ? (
        <p className="muted">{t("channel.keywordsNone")}</p>
      ) : (
        <div className="keyword-chips" role="group" aria-label={t("channel.keywords")}>
          {shown.map((k) => (
            <button
              key={k.key}
              type="button"
              className="keyword-chip"
              aria-pressed={selected.has(k.key)}
              onClick={() => onToggle(k.key)}
            >
              {k.label} <span className="keyword-count">{k.videos}</span>
            </button>
          ))}
          {hidden > 0 && (
            <button type="button" className="link" onClick={() => setShowAll(true)}>
              {t("channel.keywordsMore", { count: hidden })}
            </button>
          )}
          {showAll && keywords.length > KEYWORDS_SHOWN && (
            <button type="button" className="link" onClick={() => setShowAll(false)}>
              {t("channel.keywordsLess")}
            </button>
          )}
          {selected.size > 0 && (
            <button type="button" className="link" onClick={onClear}>
              {t("channel.keywordsClear")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The candidates table: the first checkbox-selection list in this app.
 *
 * The tick is one choice for the whole flow — what to probe, and once the
 * gate has landed, what to approve — so it is live on every row that can be
 * run at all, not only on the ones already probed. That is the fix for a
 * checkbox that read as broken: it was the approval tick, disabled until a
 * gate existed, with nothing on screen saying so.
 *
 * Four things every row has to keep apart, because collapsing any of them is
 * how a person approves something they did not mean to:
 *
 * * **A verdict from metadata and a reading from the transcript.** The first is
 *   a hypothesis about a title and the second is a quotation the code found in
 *   the words the speaker said. They are rendered differently on purpose, and a
 *   quotation the lookup did **not** find is rendered as neither. The reading
 *   gets a row of its own under the video, because it is the widest thing here
 *   and the table now shares the screen with the controls.
 * * **"Not quoted yet" and "free".** A row whose probe has not parked at a gate
 *   has no figure, and `Cost` renders that as "sin precio" rather than as zero.
 * * **"No captions" and "no information".** A video with no captions costs an
 *   Amazon Transcribe bill — measured at about twelve times a captioned one —
 *   *and* has no transcript for the topic pass to have read, so its only
 *   evidence is its title. It is flagged, and `unpickOnGate` takes its tick.
 * * **Indexed and not.** A video already on the shelf is shown with its state
 *   rather than hidden, because "you already have this" is the answer to "why
 *   can I not tick it".
 */
export function ChannelCandidates({
  rows,
  drawn,
  keys,
  picked,
  onPicked,
  busy,
}: {
  /** The whole filtered set: what the totals, "Marcar N" and the tick mean. */
  rows: Row[];
  /** The rows actually painted — `pageRows` of the above. */
  drawn: Row[];
  keys: ReadonlySet<string>;
  picked: Set<string>;
  onPicked: (next: Set<string>) => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const summary = totals(rows, picked);
  const pickable = rows.filter(selectable);

  return (
    <div className="panel">
      <div className="channel-bulk">
        <button
          type="button"
          disabled={busy || pickable.length === 0}
          onClick={() => onPicked(new Set(pickable.map((r) => r.video.videoId)))}
        >
          {t("channel.pickAll", { count: pickable.length })}
        </button>
        <button
          type="button"
          disabled={busy || picked.size === 0}
          onClick={() => onPicked(new Set())}
        >
          {t("channel.pickNone")}
        </button>
      </div>

      <table className="candidates">
        <thead>
          <tr>
            <th />
            <th>{t("channel.video")}</th>
            <th>{t("channel.verdict")}</th>
            <th>{t("channel.cost")}</th>
          </tr>
        </thead>
        <tbody>
          {drawn.map((row) => (
            <Candidate
              key={row.video.videoId}
              row={row}
              outside={outsideFilter(row, keys)}
              checked={picked.has(row.video.videoId)}
              busy={busy}
              onToggle={() => onPicked(toggle(picked, row.video.videoId))}
            />
          ))}
        </tbody>
      </table>

      <dl className="preview channel-total">
        <dt>{t("channel.selected")}</dt>
        <dd>{summary.count}</dd>
        <dt>{t("channel.totalCost")}</dt>
        <dd>
          <Cost usd={summary.usd} high={summary.usdHigh} />
        </dd>
        {/* Broken out, always. A total that hid this line would be true and
            useless: one transcribed video can cost twelve times a captioned
            one, and that is the fact somebody is deciding about. */}
        {summary.transcription !== null && (
          <>
            <dt>{t("channel.transcriptionCost")}</dt>
            <dd>
              <Cost usd={summary.transcription} high={summary.transcription} />
            </dd>
          </>
        )}
      </dl>

      {summary.withoutCaptions > 0 && (
        <p className="warn">
          {t("channel.pickedWithoutCaptions", { count: summary.withoutCaptions })}
        </p>
      )}
      {summary.unpriced > 0 && (
        <p className="warn">{t("channel.unpriced", { count: summary.unpriced })}</p>
      )}
    </div>
  );
}

function Candidate({
  row,
  outside,
  checked,
  busy,
  onToggle,
}: {
  row: Row;
  outside: boolean;
  checked: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const already = indexed(row);
  const quoted = cost(row);
  const verified = (row.reading?.temas ?? []).filter((x) => x.evidencia);

  return (
    <>
      <tr className={already || !row.video.available ? "muted" : ""}>
        <td>
          <input
            type="checkbox"
            checked={checked}
            disabled={busy || !selectable(row)}
            aria-label={row.video.title}
            onChange={onToggle}
          />
        </td>
        <td>
          <span className="candidate-title">{row.video.title}</span>
          <span className="candidate-meta">
            {duration(row.video.durationS)}
            {row.video.publishedAt ? ` · ${row.video.publishedAt.slice(0, 10)}` : ""}
            {row.gate !== null && (
              <>
                {" · "}
                {/* The source the probe actually chose, not the one anybody
                    expected. A row that fell back to transcription because the
                    caption endpoint refused this address has to be visible: that
                    is the branch that pays Amazon. */}
                <span className={hasCaptions(row) ? "" : "warn-inline"}>
                  {t(
                    hasCaptions(row)
                      ? "channel.sourceCaptions"
                      : "channel.sourceTranscribe",
                  )}
                </span>
              </>
            )}
          </span>
          {already && <span className="candidate-state">{t("channel.alreadyIndexed")}</span>}
          {/* A complete re-sync did not meet it on the playlist: deleted, or
              made private. Kept because it may be indexed; never probed. */}
          {!row.video.available && (
            <span className="candidate-state warn-inline">{t("channel.unavailable")}</span>
          )}
          {/* Shown although the filter excludes it, because it carries a run:
              a parked gate is a pending decision and hiding one would leave a
              batch approved with a row nobody could see. */}
          {outside && <span className="candidate-state">{t("channel.outsideFilter")}</span>}
        </td>
        <td>
          {row.verdict ? (
            <>
              <span className={`verdict verdict-${row.verdict.relevancia}`}>
                {t(`channel.relevance.${row.verdict.relevancia}`, {
                  defaultValue: row.verdict.relevancia,
                })}
              </span>
              {row.verdict.relevancia !== "sin_evaluar" && (
                <span className="candidate-meta">
                  {row.verdict.puntaje} · {row.verdict.razon}
                </span>
              )}
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
        <td>
          {row.gate === null ? (
            <span className="muted">—</span>
          ) : (
            <Cost usd={quoted.usd} high={quoted.high} />
          )}
        </td>
      </tr>
      {row.reading !== null && (
        <tr className={`candidate-reading${already ? " muted" : ""}`}>
          <td />
          <td colSpan={3}>
            <span className="candidate-meta">{t("channel.reading")}</span>
            {row.reading.failed ? (
              /* Read and found irrelevant, and never read at all, are different
                 facts. Only one of them says anything about the video. */
              <span className="warn-inline">{t("channel.readingFailed")}</span>
            ) : (
              <>
                <span>
                  {t(row.reading.responde ? "channel.answers" : "channel.doesNotAnswer")}
                </span>
                <ul className="candidate-topics">
                  {verified.map((topic) => (
                    <li key={topic.tema}>
                      <span className="candidate-topic">{topic.tema}</span>
                      {/* The quotation is the point: it is a span the code found
                          in the transcript, which is the only text in this table
                          the video actually contains. */}
                      <q>{topic.evidencia}</q>
                    </li>
                  ))}
                </ul>
                {row.reading.temas.length > verified.length && (
                  <span className="candidate-meta">
                    {t("channel.unverifiedTopics", {
                      count: row.reading.temas.length - verified.length,
                    })}
                  </span>
                )}
              </>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
