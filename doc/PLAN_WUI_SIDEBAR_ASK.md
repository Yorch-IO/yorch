# WUI sidebar shell and Ask three-column workspace

Built 2026-08-21. This was a plan; it is now a record of what shipped and why,
including the four places where the built thing differs from the plan.

## What was wrong

The shell rendered a horizontal `nav.tabs` inside `.app { max-width: 60rem }`.
Six tabs had grown into that bar, and two screens wanted more width than the
column allowed — `.explore-panes` collapses to a single column at exactly the
width the shell capped itself at, so Explore's three panes were **never** three
panes on any window.

`AskScreen` held one `answer` in state, so asking a second question destroyed
the first. Its citations rendered as a flat `<ol>` with the retrieved passages
folded into a `<details>` and truncated to 240 characters, which meant a claim
and the text it was drawn from could not be read at the same time. That is the
one thing a citation exists for.

## The shell

```
aside.sidebar   brand, tagline, vertical nav.tabs
header.topbar   LibraryPicker (left) · language select (right)
main.content    the six mounted screens, the only scroll container
```

`.app` is a viewport-height grid, `15rem 1fr`. Width is capped per element now —
on the prose that needs it — rather than on everything at once.

- **`min-width: 0` and `min-height: 0` on `.workspace` are load-bearing.** A grid
  item refuses to shrink below its content without them and the inner scroller
  never scrolls; the whole page grows instead.
- **Nothing may set `display` on the screen wrappers inside `main`.** All six
  screens stay mounted and hidden with the `hidden` attribute, whose `display:
  none` is a user-agent rule at the lowest specificity there is — one
  `main > div { display: … }` renders all six on top of each other. `App.test.tsx`
  asserts against this.
- **Switching tabs resets the workspace scroll.** One scroll container serving
  six screens otherwise applies the scroll position of the screen you left to
  the one you arrived at. It assigns `scrollTop` rather than calling `scrollTo`,
  because `scrollTo` is a method some environments leave out — jsdom is one, and
  the shell test threw on its first render.
- **`.explore-panes` collapses at 76rem, not 60rem.** The media query measures
  the viewport and the sidebar now takes 15rem of it before the panes see any.
  At 60rem three panes would get about 18rem each in a window that looks wide
  enough. The new `.ask-columns` shares the breakpoint.
- Below 60rem the sidebar becomes a band across the top, the tab row goes
  horizontal with `overflow-x: auto`, and the page scrolls as a page again.

## One library picker

`LibraryPicker` was rendered by each of the five screens that act on a library,
all over the same shared `useLibraries()` state — and since every screen is
mounted at once, that was five copies of one control in one document. It now
renders once, in the top bar, with a `compact` prop that lays it along the bar
instead of stacking it as a block field. Nothing is dropped in that form: the
counts and the not-ready warning are the reason it is a picker and not an id.

It is absent on Services, the one screen that owns no library and never rendered
it. The bar itself still renders there, so arriving shifts nothing below it.

## Ask

State moved out of the component into `app/src/lib/askSession.ts` as a reducer,
so the four properties that matter are properties of a function and can be
tested without rendering anything.

- History is newest-first; a submission is selected the moment it is sent, so
  the centre column shows the question being worked on rather than the previous
  answer.
- **Citation selection is stored per entry**, so returning to an earlier question
  finds it where you left it rather than reset to the first.
- **The entry records which library it was asked of.** The picker is global and
  the answers are not: an entry from before you switched libraries was answered
  by the other one, and saying so is cheaper than the confusion.
- **Selecting a history entry does not refill the composer.** The question it
  belongs to renders above its answer instead. Refilling would throw away
  whatever you were in the middle of typing.
- One question at a time, as before. Concurrent asks would mean concurrent
  unbudgeted spend, which is not something to introduce as a side effect of a
  layout change.
- History **is** persisted to localStorage (this reversed the original
  decision, at the owner's request, after the first real use). Capped at 20
  entries, guarded like `libraries.tsx` guards its own access. A question still
  in flight is persisted with the id the API gave it, so a relaunch resumes
  collecting it rather than paying for it again.

The right column selects the first citation by default and shows the passage it
rests on, joined by `chunkId` and rendered whole rather than sliced to 240
characters — the panel exists to be read against the claim. No match is a real
outcome, not an error: the citation stays and the panel says the passage behind
it is not among the ones it was handed.

## Where this differs from the plan

1. **Refusals show their passages.** The plan put an empty-state explanation in
   the citation panel for `insufficient_evidence` and `off_corpus`. Both states
   carry evidence — `off_corpus` the passages that came nearest without clearing
   the floor (`answering/service.py:35`, `e.nearby`), `insufficient_evidence` the
   ones retrieved that did not support an answer (`answering/answer.py:91`).
   Those passages are the diagnostic: they are how you tell a wrong library from
   a wrong question, and how you see what the corpus does have. Hiding them
   discards work already paid for.
2. **The picker moved to the top bar and out of all five screens**, where the
   plan kept non-Ask layouts unchanged and gave Ask its own picker.
3. **The language control stayed with the picker in the top bar** rather than
   moving into the sidebar. Both are controls for the whole app and read as a
   pair.
4. **`ask.evidence` was reused rather than replaced.** It already read
   "Retrieved passages ({{count}})", which is the refusal panel's heading, and
   the count is the first thing worth knowing there — zero means nothing came
   near, which is a different failure from six that did not answer.

## Tests

`@testing-library/react` and `jsdom` were added, which is what `CLAUDE.md`
recorded as a decision the owner had not been asked for. `vite.config.ts` now
takes `defineConfig` from `vitest/config`: `tsconfig.json` includes that file and
vite's own config type has no `test` key.

- `src/lib/askSession.test.ts` — the reducer as a plain function: newest
  submission selected while earlier entries survive, `select` restoring an
  entry's answer *and* its citation index, the `chunkId` join, and a failure
  touching only its own entry.
- `src/screens/AskScreen.test.tsx` — renders. Two questions leave two entries
  with the newest shown; clicking the older swaps the answer; clicking a second
  citation swaps the previewed passage; a citation with no matching evidence
  keeps the citation and says so; both refusals render their passages; an error
  is reported against the question that failed.
- `src/App.test.tsx` — the shell with its six screens stubbed: all mounted, one
  shown, one picker in the document, none on Services.

35 tests across 5 files. `tsc --noEmit`, `vitest run` and `vite build` pass.

**Not verified:** nothing has been looked at in the window. Three-column sizing
at a real width, the stacked layout below 60rem, and dark mode are what tests
cannot see.

## Not done, and not silently

No clear-history, no delete-entry, no export. No backend, API-type, database or
persistence change; no new Tauri command, capability or `generate_handler![]`
entry, because no new IPC surface was added.

## Second round: the answer that was thrown away

Reported after the first real use: the screen sat on «Buscando…» and ended in
«The control API did not answer», against `http://127.0.0.1:8787/ask`.

**What the evidence said.** The ports matched (`infra/.env` and the published
container both on 8787) and `/health` answered in 24 ms, so it was not the known
`ports::revalidate` defect. The API log read `POST /ask 200 OK` — twice. The API
had answered; the client had already given up at `ASK_TIMEOUT` = 180 s, and
reqwest prints "error sending request for url" for an expired timeout as well as
for a refused connection. So an answer that was computed and billed was
discarded under a message blaming the wrong component.

**The fix, across three layers.** `POST /ask` now starts the work and returns a
`question_id`; `GET /ask/{id}` collects it. `ASK_TIMEOUT` drops to 15 s because
it now covers the handover, and a new `ask_result` Tauri command polls every
1.5 s. The screen shows the seconds elapsed and says the question keeps running
if you leave — minutes of a silent spinner are indistinguishable from a hang,
which is what was reported.

**Also in this round.** History is persisted (see above). Each finished entry
carries an "ask again" button, because the composer clears on submit and a
failed question otherwise had to be retyped from memory.

**The third report — history lost when changing tabs — is not reproducible.**
`src/App.state.test.tsx` asserts against the real `AskScreen` that the history,
the answer, the half-typed question and a question in flight all survive
leaving the tab and coming back, and that returning does not re-ask. The likely
explanation is the other two: a question in flight looks like a screen that lost
its state, and a relaunch genuinely did lose the history before it was
persisted.
