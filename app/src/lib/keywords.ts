/**
 * The words a channel's titles and descriptions repeat, so a person can narrow
 * a catalogue without paying for the metadata pass.
 *
 * Descriptions as well as titles, by decision: a sermon's title is often a
 * verse and its description the subject. The description the listing carries
 * is its first 400 characters — the summary end of it, before the links and
 * service times — and the boilerplate rule below is what keeps a paragraph the
 * channel pastes under every video out of the chips.
 *
 * Pure, and kept out of the screen for the reason `channel.ts` and `radial.ts`
 * give: what is worth asserting here is the derivation — which words survive,
 * how they are counted, what a chip is called — and that is asserted on
 * numbers and strings, never on a rendered `<button>`.
 *
 * The tokenisation is a port of `docaget/docagent/bm25.py::tokenize`, including
 * its stopword list, so a keyword here is a term the lexical leg of retrieval
 * would also see. It is a fork of prose strings and nothing else; the two are
 * not required to agree byte for byte, because nothing joins them — this list
 * only decides what a person is offered to click.
 */

/** One title word, as the screen offers it. */
export interface Keyword {
  /** Lowercased and accent-folded: what the filter matches on. */
  key: string;
  /** The most frequent spelling as it appears in the titles, so the chip reads
   *  `Espíritu` and never `espiritu`. */
  label: string;
  /** How many titles carry the word — document frequency, not occurrences. A
   *  keyword is "how many videos are about it", and one title that repeats a
   *  word three times is still one video. */
  videos: number;
}

/** The list is computed to this many and shown folded at `KEYWORDS_SHOWN`. */
export const KEYWORDS_MAX = 40;
export const KEYWORDS_SHOWN = 20;

/** A word present in more than this share of the titles is boilerplate — the
 *  channel's own name, a series label, "Predicación" on a channel of
 *  predicaciones — and joins nothing, which is the one thing a filter is for.
 *  Below `BOILERPLATE_MIN_TITLES` the share is meaningless (two titles of three
 *  is 67%) and nothing is dropped. Both unmeasured against a real channel; the
 *  first real sync will say. */
export const BOILERPLATE_SHARE = 0.5;
export const BOILERPLATE_MIN_TITLES = 10;

/** Tokens under this length are dropped, as `bm25.MIN_TOKEN_LEN` drops them. */
const MIN_TOKEN_LEN = 3;

/** `bm25.py`'s `_FOLD`, ported. ñ is deliberately absent from both: folding it
 *  to n would conflate año with ano. */
const FOLD_FROM = "áàâäãåéèêëíìîïóòôöõúùûüçýÿÁÀÂÄÃÅÉÈÊËÍÌÎÏÓÒÔÖÕÚÙÛÜÇÝ";
const FOLD_TO = "aaaaaaeeeeiiiiooooouuuucyyaaaaaaeeeeiiiiooooouuuucy";
const FOLD = new Map<string, string>();
for (let i = 0; i < FOLD_FROM.length; i += 1) {
  FOLD.set(FOLD_FROM[i]!, FOLD_TO[i]!);
}

/** `bm25.py`'s `STOPWORDS`, ported verbatim. Spanish function words with no
 *  retrieval signal; one- and two-letter words need no entry because the length
 *  rule drops them first. */
export const STOPWORDS: ReadonlySet<string> = new Set(
  `con por para que los las una uno unos unas del sus esta este esto estos estas
ese esa eso esos esas aquel aquella aquello ser son era eran fue fueron sido
siendo han has hay haber habia habian hace hacer hecho puede pueden podia
podian debe deben tiene tienen tenia tenian tener como cuando donde porque
pero sino aunque mientras segun sobre entre hasta desde sin tras ante bajo
cada todo toda todos todas otro otra otros otras mismo misma mismos mismas
tan tanto tambien solo solamente muy mas menos algo alguno alguna algunos
algunas nada nadie ningun ninguna cual cuales quien quienes cuyo cuya asi
aqui alli ahora luego entonces despues antes siempre nunca aun ademas decir
dice dicen dicho les nos ello ella ellos ellas usted ustedes ver vez veces
modo manera caso pues bien cierto cierta`.split(/\s+/),
);

/** Words a YouTube catalogue carries because it is a YouTube catalogue, not
 *  because of what the videos are about. Measured on the first real channel
 *  (2,983 videos, 2026-09-16): the top forty chips held `www`, `http`, `com`,
 *  `Facebook`, `Síguenos` from the descriptions' links and social lines, and
 *  `mayo`, `agosto`, `Viernes` from the dates the titles carry. None of them
 *  is a subject anybody would search a channel for. Folded like the keys. */
export const NOISE: ReadonlySet<string> = new Set(
  `enero febrero marzo abril mayo junio julio agosto septiembre setiembre octubre
noviembre diciembre lunes martes miercoles jueves viernes sabado sabados domingo
domingos semana
www http https com org net html link enlace suscribete siguenos facebook
instagram twitter tiktok youtube whatsapp telegram spotify canal programa serie
calle carrera avenida pbx tel telefono informate inscribete`.split(/\s+/),
);

/** Links, mail addresses, handles and hashtags are removed whole before
 *  tokenising: `casaroca.org/eventos` would otherwise mint `casaroca`, `org`
 *  and `eventos`, and the third looks like a topic. Bare domains too — the
 *  description said "Más información en casaroca.org" 1,268 times with no
 *  `www.` to catch. A hashtag is the channel's branding by construction:
 *  `#CasaRocaNoPara` was the single most frequent "word" once the links were
 *  gone. `Rev.` and `Ps.` survive, because the dot is not followed by a
 *  letter. */
const LINK = /(?:https?:\/\/|www\.)\S+|[\p{L}\p{N}-]+\.[\p{L}\p{N}.-]*\p{L}{2,}(?:\/\S*)?|\S+@\S+|[@#][\p{L}\p{N}_]+/gu;

function fold(word: string): string {
  let out = "";
  for (const ch of word) out += FOLD.get(ch) ?? ch;
  return out.toLowerCase();
}

/** One title's words, each as `[key, surface]`.
 *
 *  Split on anything that is not a letter or a digit — the Unicode classes, so
 *  transliterated Greek survives the way it does in `bm25.tokenize`. A token
 *  that is digits only is dropped on top of the engine's rules: a year or a
 *  chapter number is not a topic anybody would search a channel for. */
export function tokens(title: string): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (const surface of title.replace(LINK, " ").split(/[^\p{L}\p{N}]+/u)) {
    if (surface.length < MIN_TOKEN_LEN) continue;
    if (/^\p{N}+$/u.test(surface)) continue;
    const key = fold(surface);
    if (key.length < MIN_TOKEN_LEN || STOPWORDS.has(key) || NOISE.has(key)) continue;
    out.push([key, surface]);
  }
  return out;
}

/** The frequent words of these texts — one per video, its title and its
 *  description together — most frequent first, at most `KEYWORDS_MAX`. Ties
 *  break on the label, so the order is stable across renders and the same
 *  channel always offers the same chips.
 *
 *  `exclude` is the channel's own name, tokenised: on the first real channel
 *  it sat on 20% of the titles, well under the boilerplate share, and `Casa`
 *  and `Roca` were the third and fourth chips. A channel's name is not one of
 *  its subjects whatever share of the titles it is on. */
export function titleKeywords(
  titles: string[],
  exclude: ReadonlySet<string> = new Set(),
): Keyword[] {
  const videos = new Map<string, number>();
  const spellings = new Map<string, Map<string, number>>();

  for (const title of titles) {
    const seen = new Set<string>();
    for (const [key, surface] of tokens(title)) {
      // Spellings are counted per occurrence, because what a chip should say
      // is how the channel usually writes the word; videos are counted once
      // per title, because that is what the count beside it means.
      let forms = spellings.get(key);
      if (!forms) {
        forms = new Map();
        spellings.set(key, forms);
      }
      forms.set(surface, (forms.get(surface) ?? 0) + 1);
      if (seen.has(key)) continue;
      seen.add(key);
      videos.set(key, (videos.get(key) ?? 0) + 1);
    }
  }

  const ceiling =
    titles.length >= BOILERPLATE_MIN_TITLES
      ? titles.length * BOILERPLATE_SHARE
      : Number.POSITIVE_INFINITY;

  const out: Keyword[] = [];
  for (const [key, count] of videos) {
    if (count > ceiling || exclude.has(key)) continue;
    out.push({ key, label: majority(spellings.get(key)!), videos: count });
  }
  out.sort((a, b) => b.videos - a.videos || a.label.localeCompare(b.label, "es"));
  return out.slice(0, KEYWORDS_MAX);
}

/** The spelling seen most often; the first seen on a tie, because a `Map`
 *  iterates in insertion order and that is a rule a reader can predict. */
function majority(forms: Map<string, number>): string {
  let best = "";
  let count = -1;
  for (const [surface, n] of forms) {
    if (n > count) {
      best = surface;
      count = n;
    }
  }
  return best;
}

/** Whether a video's text carries **any** of the selected keys — the any-of
 *  rule a multi-select filter means. Matched through the same tokenisation the
 *  chips were built from, so a chip always matches at least the videos it was
 *  counted on. */
export function titleMatches(title: string, keys: ReadonlySet<string>): boolean {
  if (keys.size === 0) return true;
  for (const [key] of tokens(title)) {
    if (keys.has(key)) return true;
  }
  return false;
}

/** What the topic field is set to when chips are toggled: the labels, in the
 *  order given, joined by single spaces. The field stays editable afterwards —
 *  this is a starting point, not a lock. */
export function topicOf(labels: string[]): string {
  return labels.join(" ").trim();
}
