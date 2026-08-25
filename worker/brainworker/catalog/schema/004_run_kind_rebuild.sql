-- A rebuild is not a re-index, and the cost history has to be able to say so.
--
-- A 'reindex' run re-reads the source file and pays for correction, chunking and
-- embedding; a 'rebuild' replays artifacts already paid for and can only spend
-- on embedding. Filing both under one kind would make the two indistinguishable
-- in `cost_entry`, which is the one place a user can find out where the money
-- went.
--
-- Fast and additive despite being a constraint change: the table holds one row
-- per run of a personal library, and every existing value already satisfies the
-- new predicate.
ALTER TABLE run DROP CONSTRAINT run_kind_check;
ALTER TABLE run ADD CONSTRAINT run_kind_check
    CHECK (kind IN ('preview', 'index', 'reindex', 'rebuild', 'scan'));
