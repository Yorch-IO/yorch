# Plan: Yorch, a document library with Graph RAG

## Goal

Evolve Yorch from its current *walking skeleton* into a personal,
local-first application for importing books and documents, browsing a library,
and asking questions supported only by verifiable citations. The corpus does
not include repositories or source code.

`code-graph-rag` will be the architectural reference for the graph component:
its unified schema approach, Memgraph storage, Cypher querying, and hybrid
retrieval will be adapted. Tree-sitter, AST analysis, runtime tracing, and
code-editing tools will not be reused: in Yorch, `docaget` is the structural
document parser.

## Target architecture

```text
Local files / watched folders
          |
          v
Temporal: idempotent import and re-indexing
          |
          +--> docaget: extract -> profile -> correct prose -> chunk
          |                                      |
          |                                      +--> Gemini Enterprise: concepts and relations
          |
          +--> Postgres: catalog, versions, runs, and provenance
          +--> Qdrant: chunk vectors + BM25
          +--> Memgraph: corpus structure and semantic relations
          |
          v
Local FastAPI API <-> Tauri: Library, import, folders, and questions
          |
          v
Gemini Enterprise planner -> validated read-only Cypher
          +--> Memgraph traversal + Qdrant hybrid retrieval
          +--> cited answer, or `insufficient_evidence`
```

All services remain Docker-hosted and loopback-only. The existing Postgres,
Qdrant, Temporal, and Tauri application remain in place; Memgraph is added to
the Compose stack with persistent storage and a non-public Bolt port.

## Data and graph model

Postgres is the operational source of truth; Memgraph is the queryable
knowledge projection. Every graph node has a stable identifier derived from a
document version and a reference back to the catalog.

| Node | Main data |
|---|---|
| `Library` | local library and indexing settings |
| `SourceFolder` | watched path, state, and last scan |
| `Document` | title, author, format, hash, path, and metadata |
| `DocumentVersion` | hash, timestamp, state, active version, and cost |
| `Section` | hierarchy, title, order, and source location |
| `Chunk` | text, kind, character range, page/sheet/slide, and Qdrant point |
| `Concept` | canonical name, synonyms, type, and confidence |
| `Claim` | extracted textual assertion, confidence, and source citation |
| `Citation` | verifiable locator into the original source |

Required deterministic edges are `CONTAINS`, `HAS_VERSION`, `HAS_SECTION`,
`HAS_CHUNK`, `CITES`, `NEXT`, `PREVIOUS`, and `DERIVED_FROM`. Gemini-generated
semantic edges always carry `confidence`, `extractor_model`, `created_at`, and
`source_chunk_id`: `MENTIONS`, `ABOUT`, `SUPPORTS`, `CONTRADICTS`, and
`RELATED_TO`. Low-confidence edges cannot be used to answer questions until
they are reviewed or meet the configured threshold.

## Ingestion, updates, and provenance

1. The UI supports multiple-file import and watched folders. V1 supports PDF,
   DOCX, PPTX, XLSX, CSV, TXT, and Markdown.
2. The worker hashes a file before processing. An unchanged file creates no
   duplicate vectors or nodes; a changed file creates a new `DocumentVersion`.
3. `docaget` retains its format-specific extractors, document-family profiling,
   chunking, evaluation, and position metadata. LLM correction is limited to
   prose; structured sources are never rewritten. Paid OCR requires explicit
   user confirmation.
4. Once chunks are indexed in Qdrant, document structure and citations are
   projected into Memgraph. Gemini extracts concepts, claims, and relationships
   as validated JSON, always linked to a source chunk.
5. A new version becomes active only after every projection completes. On
   failure, the prior version remains available and the run records its error,
   cost, and artifacts for retry.
6. Folder watching detects additions, changes, and deletions. A deletion marks
   its document absent while retaining history; the user explicitly chooses
   permanent removal of versions and indexes.

## Querying and answers

The local API provides text/vector search, catalog filters, document details,
and a question operation. For a question:

1. A Gemini Enterprise planner classifies the intent and selects an allowed
   graph-query template.
2. It supplies typed parameters for the template, never arbitrary Cypher. The
   server validates labels, relationships, limits, and read-only behavior before
   executing it in Memgraph.
3. Traversal supplies sections, concepts, related documents, and candidate
   chunks; Qdrant combines BM25 and vector similarity, applying per-section
   diversity and requested filters.
4. The model generates an answer exclusively from the final chunks. Every
   assertion links to one or more `Citation` records with a page, section, or
   another original location.
5. When evidence is insufficient, the API returns `insufficient_evidence`,
   explains the limitation, and displays nearby results without filling gaps
   from the model's general knowledge.

## Gemini Enterprise

Replace `docaget`'s API-key authentication with the pattern used by `yorchio`:
`google-genai` using `Client(vertexai=True, ...)` and Application Default
Credentials (ADC). Local development mounts the ADC file created by `gcloud
auth application-default login`; a managed deployment uses its runtime service
account with `roles/aiplatform.user`. API keys are not used.

Initial configuration:

- `BRAIN_GEMINI_PROJECT_ID`: Gemini Enterprise billing project.
- `BRAIN_GEMINI_LOCATION=global`: global endpoint.
- `BRAIN_GEMINI_MODEL=gemini-2.5-flash`: correction, semantic extraction,
  planning, and answer generation.
- `BRAIN_EMBEDDING_MODEL=gemini-embedding-001`: 3072-dimensional vectors.

The provider centralizes ADC resolution, quota limits, backoff retries, and
permission errors. Credentials never travel as Temporal workflow arguments and
never appear in logs, the catalog, or the UI.

## User interface and API

The Tauri app grows from its current infrastructure screen into:

- **Library:** documents, versions, format, tags, state, and re-index/delete
  actions.
- **Import and folders:** file selection, watched-folder management, OCR
  confirmation, and run progress/cost.
- **Explore:** table-of-contents navigation, concepts, and graph-related
  documents.
- **Ask:** a question, cited answer or transparent rejection, retrieved results,
  and opening the original source at its cited location.

The local API exposes resources for documents, versions, watched folders, runs,
search, graph-neighbor exploration, and questions. Response contracts include
state, provenance, and structured citations; no generated answer is returned
without at least one valid citation.

## Tests and acceptance criteria

- Index one representative document for every supported format and verify each
  citation opens the correct page, sheet, slide, or text range.
- Re-import an unchanged file without creating duplicate Qdrant points,
  Memgraph nodes, or versions; a modified file creates a new version while
  preserving the previous one until activation.
- Test watched-folder additions, modifications, deletions, and partial file
  failures.
- Verify that the projection contains the complete document hierarchy and that
  every semantic relation has a source chunk and confidence value.
- Run approved traversal queries and reject non-permitted Cypher, mutations,
  unknown labels, and unbounded results.
- Confirm grounded questions include citations and out-of-corpus questions
  return `insufficient_evidence` without fabricated content.
- Test ADC, project, global endpoint, permissions, quota, and retries through
  mocked clients; verify that no credentials enter Temporal.

## V1 boundaries

V1 is a personal local library, with no accounts, multi-user permissions, sync,
or cloud connectors. It excludes MCP, document editing, source-code graphs,
Tree-sitter, runtime telemetry, and repository ingestion. Semantic relations
remain explainable through their evidence; human review workflows and advanced
editorial contradiction detection are deferred to a later phase.
