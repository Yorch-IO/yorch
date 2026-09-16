import { useTranslation } from "react-i18next";

import { Cost } from "../Money";
import {
  cost,
  duration,
  hasCaptions,
  indexed,
  toggle,
  totals,
  type Row,
} from "../lib/channel";

/**
 * The candidates table: the first checkbox-selection list in this app.
 *
 * Four things every row has to keep apart, because collapsing any of them is
 * how a person approves something they did not mean to:
 *
 * * **A verdict from metadata and a reading from the transcript.** The first is
 *   a hypothesis about a title and the second is a quotation the code found in
 *   the words the speaker said. They are rendered differently on purpose, and a
 *   quotation the lookup did **not** find is rendered as neither.
 * * **"Not quoted yet" and "free".** A row whose probe has not parked at a gate
 *   has no figure, and `Cost` renders that as "sin precio" rather than as zero.
 * * **"No captions" and "no information".** A video with no captions costs an
 *   Amazon Transcribe bill — measured at about twelve times a captioned one —
 *   *and* has no transcript for the topic pass to have read, so its only
 *   evidence is its title. It is flagged, and `initialPicked` leaves it
 *   unticked.
 * * **Indexed and not.** A video already on the shelf is shown with its state
 *   rather than hidden, because "you already have this" is the answer to "why
 *   is it not ticked".
 */
export function ChannelCandidates({
  rows,
  picked,
  onPicked,
  busy,
}: {
  rows: Row[];
  picked: Set<string>;
  onPicked: (next: Set<string>) => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const summary = totals(rows, picked);
  const selectable = rows.filter((r) => r.gate !== null && !indexed(r));

  return (
    <div className="panel">
      <div className="channel-bulk">
        <button
          type="button"
          disabled={busy || selectable.length === 0}
          onClick={() => onPicked(new Set(selectable.map((r) => r.video.videoId)))}
        >
          {t("channel.pickAll", { count: selectable.length })}
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
            <th>{t("channel.reading")}</th>
            <th>{t("channel.cost")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Candidate
              key={row.video.videoId}
              row={row}
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
  checked,
  busy,
  onToggle,
}: {
  row: Row;
  checked: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const already = indexed(row);
  const quoted = cost(row);
  const verified = (row.reading?.temas ?? []).filter((x) => x.evidencia);

  return (
    <tr className={already ? "muted" : ""}>
      <td>
        <input
          type="checkbox"
          checked={checked}
          disabled={busy || already || row.gate === null}
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
        {row.reading === null ? (
          <span className="muted">—</span>
        ) : row.reading.failed ? (
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
                  {/* The quotation is the point: it is a span the code found in
                      the transcript, which is the only text in this table the
                      video actually contains. */}
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
      <td>
        {row.gate === null ? (
          <span className="muted">—</span>
        ) : (
          <Cost usd={quoted.usd} high={quoted.high} />
        )}
      </td>
    </tr>
  );
}
