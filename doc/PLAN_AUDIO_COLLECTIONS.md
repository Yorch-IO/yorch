# Plan: source-agnostic audio collection indexing

## Objective

Allow a user to add any public website that publishes audio. The application
catalogues every detectable audio item and keeps a local, incremental cache of
its metadata. It does **not** bulk-download audio at catalogue time.

When a user searches for a topic, an LLM evaluates the cached metadata and
returns a shortlist. Only after the user explicitly approves items from that
shortlist does the application download their media, transcribe it with AWS
Transcribe, and index the resulting transcript.

The existing YouTube workflow is the behavioural reference, but the new design
must not hard-code YouTube, Casa Roca, WordPress, or any other individual site.

## Decisions already made

| Decision | Agreed behaviour |
| --- | --- |
| Initial sync | Catalogue every audio item reachable from the collection. |
| Media during sync | Store metadata and media references only; do not download audio. |
| Scope | Public websites only. |
| Topic matching | LLM reads metadata first. |
| Import authority | A user must approve the LLM shortlist before import. |
| Transcription | AWS Transcribe. |
| Site support | Generic discovery plus per-collection configuration, not site-specific code. |

## Product flow

```text
User supplies website/archive URL
  -> discover audio collection and catalogue metadata incrementally
  -> retain local cache/index of discovered audio items
  -> user enters topic
  -> LLM screens metadata only
  -> user reviews and approves shortlist
  -> background import downloads approved audio only
  -> AWS Transcribe creates timestamped transcript
  -> existing document pipeline indexes transcript
  -> future semantic/topic queries use indexed transcripts
```

## 1. Generalize the source model

Replace channel/video-specific assumptions with reusable domain objects.

### `AudioCollection`

A user-supplied public source, such as an archive page, podcast feed, sitemap,
or media collection.

Required fields:

- stable local `collection_id`
- canonical collection URL
- display title and description
- extraction profile identifier and version
- sync state, last successful sync, catalogue completeness
- library ID used by the existing retrieval/indexing system

### `AudioItem`

One discovered public item with playable or resolvable audio.

Required fields:

- stable source key
- canonical item/page URL
- media URL or embedded-provider reference
- title, description, speaker/author, date, tags/categories, image, duration
- availability and content/metadata fingerprints
- first-seen, last-seen, and last-changed timestamps
- import, transcription, and index state

### `SourceAdapter`

An interface responsible for resolving a collection, listing items, extracting
metadata, resolving media, and refreshing content. It must not contain topic
selection, downloading policy, transcription, or indexing logic.

YouTube becomes an implementation of this generic contract. It remains useful,
but it is no longer the architecture.

## 2. Discover and catalogue audio only

The collector must inspect a website without assuming its CMS or media host.
Discovery should prefer reliable structured sources, in this order:

1. Podcast RSS/Atom feeds and enclosure URLs.
2. Site maps.
3. Archive/listing pages and pagination or next-page links.
4. Schema.org metadata, especially `AudioObject`.
5. HTML5 `<audio>` elements and direct public media URLs.
6. Recognized public embeds such as YouTube, Vimeo, SoundCloud, and podcast
   players.
7. A saved extraction profile when automatic discovery is insufficient.

Only pages with a resolvable public audio source become `AudioItem` records.
Ordinary articles without audio are not catalogued.

## 3. Per-collection extraction profiles

To support arbitrary sites without hard-coding examples, a collection can hold
a reusable extraction profile. The UI must allow the user to configure or
confirm:

- collection/listing URL pattern
- item-link selector or URL rule
- next-page selector or pagination rule
- title, description, date, speaker, tags, and image selectors
- direct audio URL or embed selector
- selectors for content to exclude

The application saves this configuration for that collection and uses it on
later syncs. A browser-rendered fallback may be added for JavaScript-only
sites, but static/feed/API discovery is the default path.

## 4. Local cache and incremental sync

Collection metadata is derived and re-fetchable, so persist it locally beside
the workspace rather than duplicating document-index state.

```text
collections/<collection-id>/
  collection.json
  items.json
  sync-state.json
  extraction-profile.json
  downloads/              # temporary/resumable import files only
```

The database remains the authority for documents, active indexed versions, and
retrieval state. The collection cache is authoritative only for crawl progress,
metadata, media references, and resumable import state.

Each item should retain:

- canonical URLs and provider IDs
- metadata fingerprint
- HTTP `ETag` and `Last-Modified`, when available
- last successful validation time
- media byte size/hash after an approved import
- partial-download offset and temporary path, when downloading
- transcript and index version/hash after processing
- an explicit error/status history

### Sync behaviour

- Save after each archive/feed page so interruption preserves prior work.
- Deduplicate by provider item ID, then canonical item URL, then normalized
  media URL, then a conservative metadata fingerprint.
- On an initially incomplete catalogue, traverse until the source ends.
- Once complete, begin from newest content and stop when an entire page is
  already known.
- A full sync ignores this early stop and reconciles items no longer available.
- Never silently drop a missing item: mark it unavailable only after a complete
  reconciliation.
- Changed metadata creates a new fingerprint and invalidates only stale
  metadata-screening results, not an already indexed transcript unless media
  itself changed.

## 5. Metadata-first topic discovery

Topic discovery is deliberately separate from import. The LLM receives only
cached metadata: title, description, speaker, tags, date, category, scripture
reference, and similar fields where present.

For every evaluated item it returns:

- `relevant`, `uncertain`, or `rejected`
- score/confidence
- a short rationale citing the metadata field that informed it
- model and prompt version

The user interface must say that this is a relevance estimate from metadata,
not evidence that the speaker discussed the topic. The evaluated input and
result are stored as a durable artifact so later results are auditable.

## 6. User-approved import only

The LLM shortlist is not an import command. The user selects and approves one
or more matching items. That approval creates durable background import jobs.

```text
resolve public media
  -> resumable download
  -> validate size/type/checksum
  -> stage media for AWS Transcribe
  -> submit and collect transcription job
  -> normalize timestamped transcript
  -> index through the existing document pipeline
  -> activate indexed version when normal validation passes
```

No item is downloaded, submitted to AWS, or indexed merely because it appeared
in a catalogue or because an LLM judged it relevant.

### Download safety and recovery

- Use temporary files and atomically promote only validated downloads.
- Resume HTTP Range downloads when the host supports them.
- Rate-limit by host and use bounded concurrency.
- Retry transient failures with exponential backoff.
- Persist progress after safe checkpoints.
- Keep failed items visible, with a retry action and an explicit reason.
- Do not re-download an unchanged item that already has a valid imported
  transcript/index version.

Temporary original audio should be removed after successful transcription and
validation by default, while transcript, provenance, and hashes remain. An
explicit retention option may preserve originals where storage policy permits.

## 7. AWS Transcribe provider

Implement AWS Transcribe behind a transcription-provider interface so neither
crawling nor indexing becomes AWS-specific.

The provider is responsible for:

- staging approved media in an appropriate S3 location
- starting asynchronous AWS Transcribe jobs
- selecting configured language settings (initially Spanish)
- monitoring job completion and collecting transcript JSON
- preserving segment/word timestamps where available
- mapping AWS failures to durable job states
- cleaning temporary S3 media and job artifacts under lifecycle rules

The indexed document includes the source page/media URL, collection ID, title,
speaker, date, tags, transcript timestamps, transcription job/model metadata,
and source/transcript hashes. Answers can therefore cite verified transcript
text and link back to the original public audio.

## 8. API and UI

Provide generic collection operations analogous to the existing channel flow:

- `POST /collections/sync`
- `GET /collections`
- `GET /collections/{collection_id}`
- `POST /collections/{collection_id}/discovery-estimate`
- `POST /collections/{collection_id}/discover`
- `POST /collections/{collection_id}/imports`
- `GET /collections/{collection_id}/jobs/{job_id}`
- `POST /collections/{collection_id}/full-sync`

The UI must distinguish these item states:

1. **Catalogued**: metadata and media reference stored; no audio downloaded.
2. **Screened**: metadata LLM result available.
3. **Approved for import**: explicitly selected by the user.
4. **Importing**: downloading, transcribing, or indexing.
5. **Indexed**: transcript is available for retrieval and topic questions.
6. **Failed / unavailable**: visible state with reason and retry/re-sync action.

Show catalogue completeness, items newly found/changed, screening scope and
cost estimate, selection count, import progress, and storage usage.

## 9. Delivery order

1. Extract generic `AudioCollection`, `AudioItem`, adapter, and local-store
   contracts from the current YouTube-specific implementation.
2. Implement persistent collection metadata, page-by-page checkpointing,
   deduplication, and incremental/full sync semantics.
3. Implement automatic public-web discovery and saved extraction profiles.
4. Port YouTube to the generic adapter contract with regression tests.
5. Generalize the metadata LLM preselection workflow and approval UI.
6. Add durable approved-import jobs and resumable public-media download.
7. Implement the AWS Transcribe provider and transcript normalization.
8. Feed approved transcripts through the existing document indexing pipeline.
9. Use Casa Roca only as a configuration/integration fixture; do not add
   Casa-Roca-specific production logic.
10. Validate against at least three kinds of public source: podcast RSS, HTML
    archive with direct audio, and an archive containing embedded media.

## 10. Acceptance criteria

- A user can add a new public audio website without a code change when generic
  extraction or a saved profile can describe it.
- The first sync catalogs every reachable audio item and survives interruption.
- A later unchanged sync does not crawl the entire archive again, download
  audio, call AWS Transcribe, or re-index anything.
- Metadata topic matching never downloads audio automatically.
- Import cannot begin until the user approves at least one shortlisted item.
- Approved imports resume after interruption and do not duplicate completed
  downloads, transcripts, or document versions.
- Indexed results retain enough provenance and timestamps to cite the original
  public audio and verify transcript evidence.
