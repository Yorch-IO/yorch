-- Structural profile collisions, recorded so the approval gate can warn.
--
-- The engine learns per-document-family profiles from a *structural*
-- fingerprint, not a topical one, so unrelated families collide and one
-- document gets chunked with another's heading rules. Retrieval metrics cannot
-- see this: the synthetic eval questions are generated from the very chunks the
-- wrong rules produced, so recall stays high while the chunking is wrong.
--
-- There is no automatic fix here, and inventing one would be worse than the
-- defect. What the app can do is show the comparison at the free gate and let a
-- person decide, which is what these rows are for.
CREATE TABLE profile_warning (
    id             bigserial PRIMARY KEY,
    version_id     text NOT NULL REFERENCES document_version(id) ON DELETE CASCADE,
    profile_id     text NOT NULL,
    -- The other family that shares this fingerprint.
    collides_with  text NOT NULL,
    similarity     double precision NOT NULL,
    detail         text NOT NULL,
    acknowledged   boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX profile_warning_version_idx ON profile_warning (version_id)
    WHERE NOT acknowledged;
