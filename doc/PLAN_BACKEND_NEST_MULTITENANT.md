# Plan: a NestJS multi-tenant backend, beside the plane that already works

## Summary

Create a NestJS service in `../yorch-tauri-backend` — its own checkout beside
this one, not a directory inside it — using `/home/kheiron/rocky-nest-backend`
as the implementation reference. It does **not** replace the FastAPI control
plane. Company Brain ships in two shapes, and each gets its own plane:

- **Free, self-managed, local** — the existing FastAPI app
  (`worker/brainworker/api/main.py`), single-tenant, no authentication, loopback
  only. Unchanged.
- **Paid, hosted, multi-tenant** — the new NestJS service. Cognito authenticates
  identity; local Postgres authorises users, memberships and the active tenant.

Both run against the same stores, the same Temporal worker and the same schema.
The desktop app gains a backend-mode setting and talks to either one.

Delivered in two phases. **Phase 1** is the plane, the identity, and `tenant_id`
through Postgres. **Phase 2** is tenant isolation in Memgraph, Qdrant and the
workspace. Splitting them is deliberate: phase 1 is verifiable on its own, and
the store rewrites in phase 2 are where the collisions are.

Full execution plan, with the file-level detail:
`~/.claude/plans/write-a-plan-for-validated-bentley.md`.

## Three corrections to the earlier draft

- **There are 26 routes, not 27.** Counted from the `@app.*` decorators in
  `worker/brainworker/api/main.py`. The 27th is FastAPI's auto-mounted `/docs`.
- **The Cognito pool in `yorch-aws-platform` is `vervux-prod-news-admins`** — a
  news admin panel's pool, one app client, no groups, `admin_create_user_only`.
  Reusing it would make every Company Brain customer a news admin, and would
  couple two products' password policy, MFA settings and user list. A dedicated
  pool is a second `module "cognito"` instantiation, which the module already
  supports through `name_prefix`.
- **Tauri does change.** It needs a backend-mode setting and a login. "Do not
  change Tauri" would have meant the desktop app could not reach the paid plane
  at all.

## Architecture and authentication

- New NestJS 11 service in `../yorch-tauri-backend`: Prisma 7 over Postgres, `@temporalio/client`,
  a Bolt client for Memgraph, Qdrant over HTTP, and the shared workspace volume
  mounted where it reads artifacts. OpenAPI at `/docs` with bearer JWT.
- **No global path prefix.** Paths stay byte-identical to FastAPI's, so the Rust
  client differs from one plane to the other by base URL and headers only.
- Validate Cognito JWTs through JWKS, RS256, `issuer` and `audience`.
  - `BRAIN_COGNITO_REGION`, `BRAIN_COGNITO_USER_POOL_ID`,
    `BRAIN_COGNITO_CLIENT_ID`. No Cognito secret is stored anywhere.
  - **ID tokens**, matching the platform's existing authorizer: only the ID token
    carries the app client id as `aud`. Any other `token_use` is rejected
    explicitly — `yorch-aws-platform/apps/frontend/src/lib/auth.ts` records that
    sending the access token instead yields a 401 that reads like a signing bug.
  - Only `GET /health/live` is public; it is the compose healthcheck. Everything
    else requires `Authorization: Bearer <token>`.
- Local authorisation in `tenant`, `app_user`, `tenant_membership`.
  - `app_user.cognito_sub` is the authoritative link; email claims an unlinked
    row once, at first login.
  - A user must exist, be active, and hold an active membership.
  - `X-Tenant-Id` selects the tenant and is checked against membership. A sole
    membership is the default; several memberships and no header is
    `400 tenant_required`.
  - Cognito groups are not consulted. The new pool has none, and local
    membership is the authority.
- Tenant context travels as an explicit argument from controller to service, as
  in the reference implementation. No AsyncLocalStorage, no request-scoped
  providers, no Prisma middleware — a silent global is the wrong place for the
  one value that decides whose data is returned.

## The error body is a client contract

`app/src/lib/api.ts` parses `detail.kind` out of the response and keys its
guidance map on it, so the NestJS exception filter emits FastAPI's shape:

```json
{"detail": {"kind": "run_not_found", "message": "...", "<extra>": ...}}
```

not the reference repository's `{statusCode, timestamp, path, message}`. All 16
existing kinds keep their current statuses, including the two that carry an extra
sibling field (`unsupported_format` with `supported`, `gate_not_ready` with
`stage`). Five are added for this plane: `unauthenticated` 401, `no_membership`
403, `user_inactive` 403, `unknown_user` 403, `tenant_required` 400.

Four compatibility quirks will not be reproduced by accident, and each gets a
test:

- `POST /ingest` takes an embedded body `{"request": …, "options": …}` because
  FastAPI embeds two body params, while `POST …/reindex` takes a bare
  `StageOptions`. The Rust client hardcodes both shapes.
- `POST /runs/{id}/approve` returns `approved` as the lowercase **string**
  `"true"`/`"false"`.
- `ProbeReport.ok`, `Answer.grounded` and `ProfileRules.needs_reextraction` are
  Python `@property` and so are absent from every response. Adding them is a
  break, not an improvement.
- `/runs/{id}` **omits** `semantics` rather than zeroing it, and `/health` and
  `/project-summary` stay 200 while the stack is half-up, reporting `null` —
  never `0` — for what could not be read. Zero is a claim about the corpus.

## How NestJS reaches the engine

The engine is Python — the Spanish BM25 tokenizer, the planner, citation
verification, the Cypher template registry. Three different answers, each chosen
for a reason:

- **Ingest, rebuild, ping, ask, removal and the provider probe go through
  Temporal.** NestJS starts workflows, reads `gate_report`/`stage` queries and
  sends the `approve` signal, over untyped handles by name.
- **`ask` becomes a real Temporal workflow, and both planes use it.** Today it
  lives in an in-process `OrderedDict`, bounded at 64 entries with a 3600s TTL,
  reaped only on `POST /ask`, and safe *only* because uvicorn runs a single
  worker. A new `AskWorkflow` exposes a `result` query and `question_id` becomes
  the workflow id. FastAPI switches to it too, keeping its response bodies — which
  also deletes the single-worker constraint from the free plane.
- **Removal goes through Temporal rather than being ported.** `removal.py` owns a
  documented ordering — projections first, catalog last, so a crash leaves the
  operation retryable — and re-implementing it in TypeScript re-implements that
  rule. Latency does not matter for a confirm-gated destructive action.
- **Graph reads are ported to TypeScript, and only eleven of them.** The read
  surface uses eleven templates from `graph/queries.py`. The planner never sees
  the TypeScript registry — it runs inside `ask`, which stays in Python — so the
  boundary that matters ("a planner returns a template id, never Cypher") is not
  forked. What is forked is a set of Cypher strings, guarded two ways: the
  validator and `bind()` are ported with it and run at module load, and a parity
  test asserts the TypeScript registry's ids and parameter specs match a fixture
  dumped from `catalogue()`. `Graph.write` gets no TypeScript equivalent.

## Schema ownership moves to Prisma

Today the FastAPI lifespan applies numbered SQL files through a hand-written
runner, and `worker` waits on `api` being *healthy* purely to prove the schema is
current — a worker waiting on an HTTP server to establish a database fact.

- The existing schema is introspected and baselined into the new checkout's
  `prisma/` directory. The
  four SQL files stay on disk as the historical record; nothing reads them.
- `catalog/migrations.py` becomes an **assertion**: it reads the applied
  migrations and refuses a schema older than the code expects. It applies nothing.
- A one-shot `migrate` service runs `prisma migrate deploy`. Both `api` and
  `worker` depend on it having *completed successfully*, and the
  `worker → api: service_healthy` edge is removed. Free-mode deployments run the
  same step, which is what keeps the two planes from ever disagreeing about the
  schema.

## Two checkouts, and what crosses between them

The control plane is a sibling directory rather than a subdirectory, so three
things now reach across a boundary that nothing enforces. Each is configurable
and each fails with a message naming what it looked for, because a wrong
assumption about someone else's disk should not read as a broken build:

| What | Default | Override |
|---|---|---|
| Compose build context for `migrate`/`backend` | `../../yorch-tauri-backend` | `BRAIN_BACKEND_CONTEXT` |
| Where the worker's test-only `apply_migrations` reads DDL | the sibling checkout's `prisma/migrations` | `BRAIN_MIGRATIONS_DIR` |
| Where the parity fixture's dumper finds the Python registry | `../yorch` | `YORCH` |

The Python catalog suite **skips**, naming the directory it wanted, when the
migrations are not reachable — a clone of this repository alone genuinely cannot
build a schema, and that is a missing prerequisite rather than a failing test.


## Multi-tenancy in the catalog

`tenant_id` is added to `library`, `source_folder`, `document`,
`document_version`, `run`, `run_artifact`, `cost_entry` and `profile_warning`,
with a column **default** pointing at a legacy tenant created by an idempotent
migration.

The default is the whole trick: roughly thirty `INSERT` statements in
`catalog/repo.py` keep working untouched, so the free plane needs no Python
changes in phase 1. NestJS always sets `tenant_id` explicitly and never relies on
it. **Phase 2 drops the default** once the worker threads a tenant through its
activities — until then it is a temporary crutch, because a writer that forgets
`tenant_id` lands silently in the legacy tenant instead of failing.

Unique constraints widen with it. `document_version.content_sha256 UNIQUE`
becomes `UNIQUE (tenant_id, content_sha256)`: "one `DocumentVersion` per distinct
sha256" becomes *per tenant*, and two customers importing the same PDF get two
versions. `run.workflow_id` stays globally unique — ids are minted centrally and
must not repeat.

## Phase 2: the stores that have no isolation yet

Phase 1 scopes Qdrant and Memgraph only by resolving ownership in Postgres first.
That is a real boundary and a thin one. Two findings make phase 2 more than a
filter:

- **`Concept` must be salted, not merely filtered.** `concept_id` is a pure hash
  of the canonical name, so "Dios" is one node for the whole database — and
  `Concept.description_raw` accumulates text from every chunk that mentions it.
  Two tenants would share one node carrying both their content.
- **Point ids collide across tenants today.** A Qdrant point id is
  `uuid5(ns, f"{version_id}:{index}")` and the version id derives from content, so
  the same file imported by two customers lands on the same point. Version id
  derivation must include the tenant.

Also in phase 2: `tenant_id` on `Document`, `DocumentVersion` and `Chunk` with the
filter in every template and in `retrieve.py`'s literal hydration Cypher;
`tenant_id` in the Qdrant payload and payload indexes, injected in one place and
never accepted from a caller; a per-tenant workspace root with the containment
check `_safe_run_dir` already implements; `tenant_id` on `IngestRequest` and
through the activities that write to the catalog; and `tnt` added to the id regex.

The acceptance test for the phase: index the same file under two tenants and
confirm two of everything, in all four stores.

## Docker and configuration

- `infra/docker-compose.yaml` gains the `migrate` service and a `backend` service
  on `127.0.0.1:${BRAIN_BACKEND_HOST_PORT:-8788}`, behind a compose profile so a
  free-mode stack does not start it. Every port stays loopback-bound.
- The Cognito settings reach the container through **`infra/cognito.env`**, not
  through `.env`. `.env` is not a place to put anything: the desktop app rebuilds
  it from scratch on every launch (`app/src-tauri/src/stack.rs:170`), so a pool
  id added there survives until the next time somebody opens the app. The file
  is declared `required: false`, so a free-mode stack that never starts this
  service does not need it — and a paid start without it refuses to boot naming
  the variables, which is a better failure than a 401 that reads like a signing
  problem. There is no Cognito credential to mount: a pool id and a client id
  identify a pool and authorize nothing.
- Consume the new pool's Terraform outputs from `yorch-aws-platform`; deploy no
  API Gateway or Lambda for this backend. The new app client needs
  `ALLOW_ADMIN_USER_PASSWORD_AUTH` — the news client is SRP-only, and without an
  admin password flow there is no way to mint a token from the CLI to check a
  protected route by hand.

## Tauri

- A backend-mode setting: `local` (FastAPI, today's behaviour, no headers) and
  `cloud` (NestJS, bearer plus `X-Tenant-Id`). `control.rs` already centralises
  request construction, so the mode picks a base URL and a header set.
- Hosted-UI login: authorization-code with PKCE in the system browser, tokens in
  the OS keychain beside the provider keys, refreshed 60s early.
  `yorch-aws-platform/apps/frontend/src/lib/auth.ts` is ~90 lines doing exactly
  this — port it rather than adding an Amplify dependency.
- Opening the hosted UI needs a permission **pair**: `opener:allow-open-url` and
  `opener:allow-default-urls`. Granting only the first denies every URL at runtime
  with no visible error.

## Testing and acceptance

- Unit: token validation (wrong issuer, wrong audience, `token_use: "access"`,
  expired, unknown kid), unknown and inactive users, tenant selection in all four
  cases, the ported template validator's every rejection reason, `bind` coercion
  and cap clamping, and the error filter's shape.
- Integration across all 26 routes: `401` without a token, `403` without
  membership, `400` when a multi-tenant user omits the header, and two seeded
  tenants with overlapping data where every cross-tenant id returns **404, not
  403** — a 403 confirms that another tenant's id exists.
- Tokens are minted locally against a test JWKS. No test reaches Cognito.
- Postgres is provisioned as a throwaway *schema* via `search_path`, dropped
  `CASCADE`, skipping with the URL it tried when the database is down — the
  pattern `worker/tests/catalog/conftest.py` already uses. The reference
  repository's own suite is scaffold stubs and is not the model.
- Python gains workflow tests for `AskWorkflow`, `RemovalWorkflow` and
  `probe_provider`, and a test that the schema assertion refuses an old schema.
  **The existing `worker/tests/api/` suite must pass unchanged** — it is the proof
  the free plane did not move.
- Anything that writes points sets `BRAIN_QDRANT_COLLECTION` to a disposable name.
  Leftover test points once outnumbered real ones 105 to 5 and turned eight
  retrieval tests into silent skips.
- Verify a live estimate against the deployed image, not the repository: a
  constant corrected in the source changes nothing at a real gate until the image
  is rebuilt, and `--build` is a silent no-op without `docker-compose.dev.yaml`.
- **Anything that reaches Vertex needs `docker-compose.adc.yaml` too.** Without
  it both planes answer `provider_unavailable` with a `DefaultCredentialsError`,
  and `/health` still reports the provider row green — deliberately, since that
  row is free and only says whether a project id is set. `POST /provider/probe`
  is what spends and therefore what actually knows. The full command is
  `-f docker-compose.yaml -f docker-compose.dev.yaml -f docker-compose.adc.yaml`;
  the app applies the third conditionally, so a stack started by hand does not
  get it.

## Fixed assumptions

- Cognito is the remote identity directory; local Postgres is the tenancy source
  of truth.
- Tenants and memberships are provisioned by migrations and seeds. No admin UI or
  admin API in this delivery: create the user in Cognito through
  `yorch-aws-platform` scripts, then insert the local user and membership.
- The free plane stays single-tenant forever. It always writes the legacy tenant.
