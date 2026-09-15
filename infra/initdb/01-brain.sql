-- The app catalog database.
--
-- Temporal's auto-setup image creates `temporal` and `temporal_visibility`
-- itself when it first connects, so this script only has to add the database
-- the app's own catalog lives in. Schema creation belongs to the one-shot
-- `migrate` service, which runs `prisma migrate deploy` from the backend image
-- before either control plane starts. It used to be the API's job — a lifespan
-- hook applying numbered SQL files — and that stopped being tenable the moment
-- two planes shared this catalog: exactly one thing may own a schema. Both
-- planes now only *check* the version and report it on `/health`.
--
-- Runs once, on an empty data directory. Changing it later has no effect on an
-- existing volume.

CREATE DATABASE brain;
