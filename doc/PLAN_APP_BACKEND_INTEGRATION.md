# Local and Cognito Backend Integration Plan

## Summary

Complete and validate the existing integration between `yorch` and
`yorch-tauri-backend` without changing local-mode behavior. Pre-production will
run as an integrated localhost stack; this delivery does not publish an API or
design production infrastructure.

## Integration changes

- Keep `local` as the default mode: local FastAPI, local Docker stack, no
  authentication, and no authorization headers.
- Consolidate `cloud` as the paid mode: the Tauri client targets the local
  NestJS endpoint, retrieves configuration from `GET /auth/config`, signs in
  with Cognito Authorization Code + PKCE, and stores the session outside the
  webview.
- Support one active organization per user in this first release:
  - Do not add a memberships endpoint or organization picker.
  - Hide or remove the manual `X-Tenant-Id` field from the UI; the backend
    resolves the user's sole membership.
  - Retain backend validation for multiple memberships as future protection,
    but do not provision such users in this release.
- Preserve the contract for all 26 routes across FastAPI and NestJS: payloads,
  HTTP status codes, omitted fields, and the `detail.kind` error body.
- Use `/uploads` only in cloud mode and local staging in local mode; ensure
  imports and filesystem paths never cross tenant boundaries.
- Complete application guidance for every authentication and tenancy error that
  NestJS can return.

## Pre-production and provisioning

- Start shared services, Prisma migrations, and the Compose `paid` profile on
  the validation machine, exposing NestJS only at `127.0.0.1:8788`.
- Populate `infra/cognito.env` from the dedicated Cognito pool's Terraform
  outputs, and keep the registered callback fixed at
  `http://localhost:8789/auth/callback`.
- Verify that the backend refuses to start without Cognito identifiers, Swagger
  is disabled, and no credential is stored in `infra/.env`.
- Define an operator runbook for every customer setup: create the Cognito user,
  tenant, `app_user`, and one active `tenant_membership`; verify that the first
  sign-in links the Cognito subject.
- Document explicit boundaries: this environment does not validate remote
  desktop access, public HTTPS, self-service sign-up, or production operation.

## Tests and acceptance criteria

- Run type checking and test suites for `app`, `worker`, and
  `yorch-tauri-backend`; add or complete contract tests for every local/cloud
  difference.
- Verify that local mode neither sends `Authorization` nor requires Cognito,
  and cloud mode does not prepare or start Docker from the desktop app.
- Validate the localhost pre-production path end to end: configure cloud mode,
  sign in with Cognito, upload a document, approve and index it, ask a question,
  sign out, and confirm requests are rejected without a session.
- Test isolation with at least two tenants at backend level: catalog data,
  uploaded files, graph data, and vector data must remain inaccessible across
  tenants. The desktop client only operates users with one membership in this
  release.
- Cover failure cases: expired or unrefreshable token, valid Cognito identity
  without local provisioning, inactive user, no active membership, incomplete
  Cognito configuration, and invalid PKCE callback/state.

## Assumptions

- Existing uncommitted changes in both checkouts are preserved.
- The dedicated Cognito pool in `yorch-aws-platform` is the identity provider;
  Postgres remains the authorization source.
- Local mode remains free, self-managed, and usable without an account or AWS
  services.
