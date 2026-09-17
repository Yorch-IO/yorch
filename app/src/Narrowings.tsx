import { useTranslation } from "react-i18next";

import type { AskNarrowings } from "./lib/api";

/** The four narrowings a recording corpus makes askable, as one fieldset.
 *
 *  Rendered only when the selected library is a bucket's — every document in
 *  it carries a recording date and a source, so the filters have something
 *  to narrow on; a shelf of books has neither, and a date field over it would
 *  narrow to nothing while looking like a control. Each is a hard cut over
 *  what the dense floor already admits, and the caveat says so: `off_corpus`
 *  fires more readily under a filter than without one.
 *
 *  `fieldset.stages` is reused verbatim, like the effort control, so this has
 *  no layout of its own to break at a narrow width. */
export function Narrowings({
  value,
  onChange,
  disabled,
  sources = [],
}: {
  value: AskNarrowings;
  onChange: (next: AskNarrowings) => void;
  disabled: boolean;
  /** The source names the library holds, when the screen knows them. */
  sources?: string[];
}) {
  const { t } = useTranslation();
  const set = (patch: AskNarrowings) => onChange({ ...value, ...patch });
  return (
    <fieldset className="stages narrowings">
      <legend>{t("ask.narrowLegend")}</legend>
      <label className="field field-inline">
        <span>{t("ask.narrowFrom")}</span>
        <input
          type="date"
          value={value.recorded_from ?? ""}
          disabled={disabled}
          onChange={(e) => set({ recorded_from: e.target.value })}
        />
      </label>
      <label className="field field-inline">
        <span>{t("ask.narrowTo")}</span>
        <input
          type="date"
          value={value.recorded_to ?? ""}
          disabled={disabled}
          onChange={(e) => set({ recorded_to: e.target.value })}
        />
      </label>
      <label className="field field-inline">
        <span>{t("ask.narrowScripture")}</span>
        <input
          type="text"
          value={value.scripture ?? ""}
          placeholder={t("ask.narrowScripturePlaceholder")}
          disabled={disabled}
          onChange={(e) => set({ scripture: e.target.value })}
        />
      </label>
      <label className="field field-inline">
        <span>{t("ask.narrowSource")}</span>
        {sources.length > 0 ? (
          <select
            value={value.source_name ?? ""}
            disabled={disabled}
            onChange={(e) => set({ source_name: e.target.value })}
          >
            <option value="">{t("ask.narrowAnySource")}</option>
            {sources.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        ) : (
          <input
            type="text"
            value={value.source_name ?? ""}
            disabled={disabled}
            onChange={(e) => set({ source_name: e.target.value })}
          />
        )}
      </label>
      <p className="caveat">{t("ask.narrowCaveat")}</p>
    </fieldset>
  );
}

/** Only the narrowings that are set, so an empty form sends nothing. */
export function activeNarrowings(value: AskNarrowings): AskNarrowings {
  const out: AskNarrowings = {};
  if (value.recorded_from) out.recorded_from = value.recorded_from;
  if (value.recorded_to) out.recorded_to = value.recorded_to;
  if (value.scripture?.trim()) out.scripture = value.scripture.trim();
  if (value.source_name?.trim()) out.source_name = value.source_name.trim();
  return out;
}
