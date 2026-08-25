-- The operational source of truth: what exists, where it came from, what it
-- cost, and which version a question is allowed to see.
--
-- Memgraph and Qdrant are both projections of this. Either can be dropped and
-- rebuilt from these rows plus the original files; neither can be reconstructed
-- from the other. That asymmetry is why provenance and cost live here and
-- nowhere else.
--
-- Ids are text, not uuid, and are the *same strings the graph uses*
-- (`doc_…`, `ver_…`). A join across the two stores is then a string equality
-- rather than a mapping table that can drift.

CREATE TABLE library (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    -- Content language, which is not the UI language: a Spanish corpus browsed
    -- by an English-speaking user is the normal case, not an edge case.
    language      text NOT NULL DEFAULT 'es',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_folder (
    id            text PRIMARY KEY,
    library_id    text NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    path          text NOT NULL,
    -- Watching is opt-in per folder so importing a directory once does not
    -- commit the user to re-indexing it forever.
    watch         boolean NOT NULL DEFAULT true,
    state         text NOT NULL DEFAULT 'idle'
                  CHECK (state IN ('idle', 'scanning', 'error')),
    last_scan_at  timestamptz,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (library_id, path)
);

CREATE TABLE document (
    id            text PRIMARY KEY,
    library_id    text NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    folder_id     text REFERENCES source_folder(id) ON DELETE SET NULL,
    -- Library-relative, so moving the library root does not orphan every row.
    source_key    text NOT NULL,
    title         text NOT NULL,
    author        text,
    format        text NOT NULL,
    -- A deleted file keeps its history. Permanent removal is a separate,
    -- explicit act by the user, because a folder unmounted for a minute must
    -- not destroy an indexed corpus.
    present       boolean NOT NULL DEFAULT true,
    absent_since  timestamptz,
    tags          text[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (library_id, source_key)
);

CREATE INDEX document_library_present_idx ON document (library_id, present);

-- One row per distinct byte sequence, not per (document, import). Two identical
-- files therefore share a version, and the expensive per-content work —
-- correction, chunking, embedding, extraction — happens once. This is the fix
-- for docagent's `doc_id_for()`, which hashes the path and so indexes byte
-- identical duplicates twice, where they then compete in ranking.
CREATE TABLE document_version (
    id              text PRIMARY KEY,
    content_sha256  text NOT NULL UNIQUE,
    byte_size       bigint NOT NULL,
    page_count      integer,
    state           text NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'previewed', 'approved',
                                     'indexing', 'indexed', 'failed')),
    -- Set only once every projection has completed. Questions read active
    -- versions; a half-projected one is invisible rather than wrong.
    activated_at    timestamptz,
    failed_reason   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Many-to-many on purpose: the same bytes can sit at two paths, and each path
-- is its own document slot in the library.
CREATE TABLE document_version_link (
    document_id   text NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    version_id    text NOT NULL REFERENCES document_version(id) ON DELETE CASCADE,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, version_id)
);

-- Exactly one active version per document, enforced by the database rather than
-- by application discipline: "which version does this citation refer to" has to
-- have one answer even after a crash mid-activation.
CREATE TABLE document_active_version (
    document_id   text PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    version_id    text NOT NULL REFERENCES document_version(id) ON DELETE RESTRICT,
    activated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE run (
    id              text PRIMARY KEY,
    workflow_id     text NOT NULL UNIQUE,
    document_id     text REFERENCES document(id) ON DELETE SET NULL,
    version_id      text REFERENCES document_version(id) ON DELETE SET NULL,
    kind            text NOT NULL
                    CHECK (kind IN ('preview', 'index', 'reindex', 'scan')),
    state           text NOT NULL DEFAULT 'running'
                    CHECK (state IN ('running', 'awaiting_approval',
                                     'succeeded', 'failed', 'cancelled')),
    stage           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    error_kind      text,
    error_detail    text
);

CREATE INDEX run_state_started_idx ON run (state, started_at DESC);

-- Payload-by-reference. Temporal payloads cap around 2 MB and history persists
-- for the whole retention period, so activities return one of these instead of
-- the bytes: relative path, digest, size.
CREATE TABLE run_artifact (
    run_id        text NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    name          text NOT NULL,
    rel_path      text NOT NULL,
    sha256        text NOT NULL,
    size_bytes    bigint NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, name)
);

-- Measured token counts with a price multiplier applied. The multiplier is a
-- third-party figure and can be wrong or stale; the token counts are not. Any
-- surface showing a dollar figure has to repeat that caveat, which is why the
-- two are stored in separate columns rather than as one precomputed total.
CREATE TABLE cost_entry (
    id            bigserial PRIMARY KEY,
    run_id        text NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    stage         text NOT NULL,
    provider      text NOT NULL,
    model         text NOT NULL,
    input_tokens  bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    -- NULL means "we have token counts but no price for this model", which is
    -- an honest answer the UI can render. Zero would be a lie.
    usd           numeric(12, 6),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX cost_entry_run_idx ON cost_entry (run_id);
