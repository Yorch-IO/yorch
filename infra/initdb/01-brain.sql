-- The app catalog database.
--
-- Temporal's auto-setup image creates `temporal` and `temporal_visibility`
-- itself when it first connects, so this script only has to add the database
-- the app's own catalog lives in. Schema creation is the API's job: it runs the
-- SQL migrations in brainworker/catalog/schema/ on startup, so the schema
-- version always matches the code that reads it.
--
-- Runs once, on an empty data directory. Changing it later has no effect on an
-- existing volume.

CREATE DATABASE brain;
