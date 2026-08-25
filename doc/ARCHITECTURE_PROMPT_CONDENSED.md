# Agnostic Multi-Platform Application Architecture Template

Use this prompt to generate new applications with a proven, scalable architecture. Replace domain-specific terms with your application's concepts while maintaining the structural integrity.

---

## Quick Start Template

**Domain Context**:
- Replace `Recording` with your primary entity (e.g., `Document`, `Project`, `Campaign`)
- Replace `User` with your authentication model
- Replace `Workspace` with your collaboration scope
- Replace `Share` with your distribution mechanism

---

## Repository Structure Template

```
├── apps/
│   ├── desktop/          # Native desktop (Tauri + Solid.js)
│   ├── web/              # Web server & dashboard (Next.js + React)
│   ├── cli/              # CLI tool (Rust)
│   ├── mobile/           # Mobile app (React Native)
│   └── [extensions]/     # Browser/integrations
│
├── packages/
│   ├── database/         # Schema + ORM (Drizzle)
│   ├── web-backend/      # Server logic (Effect.js)
│   ├── web-domain/       # Shared types
│   ├── ui/               # React components
│   ├── ui-solid/         # Solid.js components
│   ├── utils/            # Shared utilities
│   ├── sdk-[domain]/     # Public SDK for developers
│   └── env/              # Configuration & validation
│
├── crates/               # Rust modules for compute
│   ├── [core-engine]/    # Core business logic
│   ├── [processing]/     # CPU-intensive work
│   └── platform-*/       # Platform-specific code
│
└── scripts/ + infra/     # Deploy & setup
```

---

## Core Pattern: Effect.js Typed APIs

Every backend endpoint follows this pattern:

```typescript
// 1. Define schema and types
export const DomainTable = mysqlTable("domain", {
  id: varchar("id", { length: 128 }).primaryKey(),
  userId: varchar("user_id").references(() => users.id),
  // ... fields
});

// 2. Define service function
export const GetDomainEndpoint = Effect.gen(function* () {
  const db = yield* Database;
  const auth = yield* AuthContext;
  const domain = yield* db.query(/* ... */);
  return domain;
});

// 3. Expose as API route
export const handler = apiToHandler(
  ApiBuilder.get("/api/domain/:id", GetDomainEndpoint)
    .pipe(Layer.provide(DomainServiceLive))
);

// 4. Types automatically available to clients
useEffectQuery("getDomain", GetDomainEndpoint);
```

**Advantages**:
- Type inference from database → API → UI
- Automatic error handling
- Dependency injection
- Composable services
- No schema duplication

---

## Data Layer Pattern

```typescript
// packages/database/schema.ts
export const [entities] = mysqlTable("[entities]", {
  id: varchar("id", { length: 128 }).primaryKey(),
  // owned_by: relation pattern
  workspaceId: varchar("workspace_id").references(() => workspaces.id),
  // status: finite state
  status: varchar("status", { enum: ["draft", "active", "archived"] }),
  // audit: timestamps
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").onUpdateNow(),
});

// Workflow
1. Schema defined in TypeScript
2. pnpm db:generate → SQL migrations auto-created
3. pnpm db:push → Applied to database
4. Types generated automatically
```

---

## Multi-Platform UI Pattern

**Principle**: Maximize code sharing, minimize duplication

```
packages/
├── ui/                   # React components (web + extensions)
│   ├── Button.tsx        # "import Button from '@cap/ui'"
│   ├── Modal.tsx
│   └── ...
│
├── ui-solid/             # Solid.js components (desktop)
│   ├── Button.tsx        # Same design system, different library
│   ├── Modal.tsx
│   └── ...
│
└── web-domain/           # Shared types (all platforms)
    ├── types.ts          # interface User, interface Workspace
    └── validation.ts     # Shared Zod schemas
```

**Pattern**:
- Design system in Tailwind (language-agnostic)
- Components per-framework, same class names
- Shared domain types in web-domain
- Minimal platform-specific logic

---

## Rust Integration Pattern

Use Rust when:
- CPU-bound: transcoding, processing, analysis
- Performance-critical: encoding, compression
- Platform-specific: device access, OS integration
- Shipping as library: SDKs, plugins

**Example Crate Structure**:
```rust
// crates/video-processing/src/lib.rs
pub async fn process_video(input: &Path) -> Result<ProcessedVideo> {
  let frames = extract_frames(input)?;
  let optimized = optimize_frames(frames)?;
  Ok(ProcessedVideo { optimized })
}

// Call from Node.js via Tauri or Node native binding
// Export metrics to OpenTelemetry
```

---

## Deployment Architecture

### Single Server (Docker Compose)
```yaml
services:
  app:           # Next.js + API
  worker:        # Background jobs (media server)
  db:            # MySQL
  storage:       # S3-compatible (MinIO)
  cache:         # Redis
```

### Multi-Server (Kubernetes)
```
Load Balancer
├── app-1, app-2, app-3          # Horizontal scaling
├── worker-1, worker-2, worker-3 # Job processing
├── db (primary + replicas)      # Database cluster
└── storage (distributed)         # S3 multi-region
```

### Serverless (AWS Lambda + CDN)
```
CloudFront CDN
├── Lambda functions (Next.js)
├── API Gateway
├── RDS (MySQL)
└── S3 storage
```

---

## API Design Pattern

**Resources over Actions**:
```
❌ POST /api/startProcessing
✅ POST /api/items + { status: "processing" }

❌ GET /api/getItemCount
✅ GET /api/items?limit=0 + content-length header

❌ POST /api/archiveAllOldItems
✅ PATCH /api/items/search?createdBefore=2024-01-01 + { status: "archived" }
```

**Error Codes**:
```typescript
// Domain-specific errors
export class ItemNotFound extends TaggedError("ItemNotFound") {}
export class WorkspaceQuotaExceeded extends TaggedError("WorkspaceQuotaExceeded") {}

// Map to HTTP
ItemNotFound → 404
WorkspaceQuotaExceeded → 402 (Payment Required)
```

---

## Code Generation from Types

**Direction**: Database Schema → Types → API → UI

```bash
# 1. Define schema
packages/database/schema.ts: export const items = mysqlTable(...)

# 2. Generate
pnpm db:generate → generates index.ts with full types

# 3. Use in backend
packages/web-backend/services.ts: function getItem(id: string): Effect<Item>

# 4. Expose as API
apps/web/app/api/items/[id].ts: export const handler = apiToHandler(...)

# 5. Query in frontend
apps/web/components/ItemDetail.tsx: useEffectQuery("getItem", { id })
```

**Result**: No manual type updates ever needed.

---

## Testing Strategy

```typescript
// Unit: Business logic
test("calculateDiscount returns correct percentage", () => {
  expect(calculateDiscount(100, 0.1)).toBe(90);
});

// Integration: Database + API
test("createItem stores in DB and returns via API", async () => {
  const item = await api.items.create({ name: "Test" });
  const stored = await db.query.items.findById(item.id);
  expect(stored.name).toBe("Test");
});

// E2E: Full flow
test("user can create, view, and share item", async () => {
  const app = await browser.start();
  await app.click("Create");
  await app.fill("Name", "My Item");
  await app.click("Save");
  expect(await app.url()).toContain("items/");
});
```

---

## Development Workflow

```bash
# ONE-TIME SETUP
pnpm install
pnpm env-setup        # Interactive: create .env with secrets
pnpm cap-setup        # Download deps, start Docker services

# DAILY DEVELOPMENT
pnpm dev              # Starts web, desktop, services
# OR
pnpm dev:web          # Just web for faster iteration

# After schema changes
pnpm db:generate      # Auto-generate types

# Before committing
pnpm lint --write     # Fix formatting
pnpm typecheck        # Catch type errors
pnpm test             # Run test suite

# For release
pnpm build            # Full workspace build
pnpm tauri:build      # Desktop binary (if applicable)
```

---

## Feature Addition Checklist

When adding a new feature:

- [ ] **Schema**: Add tables/fields to `packages/database`
- [ ] **Generate**: Run `pnpm db:generate`
- [ ] **Types**: Update `packages/web-domain` if needed
- [ ] **API**: Create endpoint in `apps/web/app/api`
- [ ] **Backend**: Add service to `packages/web-backend`
- [ ] **UI**: Add component to `packages/ui` (React) + `packages/ui-solid` (Desktop)
- [ ] **Desktop**: If needed, add Rust code to `crates/`
- [ ] **Tests**: Add integration test covering happy path
- [ ] **Docs**: Update API docs / user guide
- [ ] **Migration**: Add database migration if breaking
- [ ] **Lint**: Run `pnpm lint --write && pnpm typecheck`

---

## Extension Points

### Add New App/Platform
1. Create `apps/[platform]/package.json`
2. Import from `packages/` (UI, domain, utils)
3. Add to `docker-compose.yml` if it's a service
4. Register in `turbo.json` for build orchestration

### Add New Service
1. Create `packages/[service]/package.json`
2. Export types and main function
3. Re-export from `packages/index.ts`
4. Use with: `import { [Service] } from "@cap/[service]"`

### Add New API Endpoint
1. Define in `packages/web-api-contract`
2. Implement in `packages/web-backend`
3. Create route in `apps/web/app/api/[...route]`
4. Expose in web builder
5. Types automatically inferred in UI code

---

## Performance Optimization Checklist

- **Database**: Indexes on foreign keys, common filters
- **API**: Pagination, field selection, caching headers
- **UI**: Code splitting, lazy loading, image optimization
- **Network**: Compression, CDN for assets, edge caching
- **Server**: Connection pooling, query batching
- **Rust**: Profile with flamegraph, optimize hot paths

---

## Security Considerations

- **Auth**: JWT/OAuth with secure refresh tokens
- **RBAC**: Row-level security in database
- **API**: Rate limiting, CSRF protection, input validation
- **Data**: Encryption at rest, TLS in transit, secrets management
- **Deployments**: Container scanning, dependency audits, SBOM

---

## Monitoring & Observability

```typescript
// Automatic instrumentation
export const handler = apiToHandler(
  ApiBuilder.get("/api/item/:id", GetItem)
    .pipe(
      Effect.tap((result) => 
        Metrics.recordHistogram("item.fetch_ms", Date.now() - start)
      ),
      Layer.provide(TracingLayer)  // Auto-spans
    )
);

// Export to: Prometheus, Datadog, New Relic, etc.
// Logs: structured JSON to stdout
// Traces: OpenTelemetry protocol
```

---

## Configuration Management

```typescript
// packages/env/index.ts
export const env = z.object({
  // Required
  DATABASE_URL: z.string().url(),
  JWT_SECRET: z.string().min(32),
  
  // Optional with defaults
  LOG_LEVEL: z.enum(["debug", "info", "warn", "error"]).default("info"),
  WORKER_THREADS: z.number().int().positive().default(4),
  
  // Feature flags
  FEATURE_BETA_UI: z.boolean().default(false),
}).parse(process.env);

// Usage in code
import { env } from "@cap/env";
const dbUrl = env.DATABASE_URL;  // Type-safe, validated
```

---

## Recommended Tech Stack

**Frontend**:
- React (web, extensions)
- Solid.js (desktop)
- TypeScript, Tailwind CSS
- Tanstack Query for data fetching

**Backend**:
- Next.js (web server)
- Effect.js (typed APIs)
- Drizzle ORM (database)

**Desktop**:
- Tauri v2 (native wrapper)
- SolidStart (framework)
- Rust backend

**Mobile** (if needed):
- React Native / Expo
- Same domain models as web

**Infrastructure**:
- Docker Compose (local)
- Kubernetes or serverless (production)
- MySQL (database)
- S3-compatible (storage)
- Redis (cache/sessions)

---

## Summary: 12-Point Checklist for Implementation

1. ✅ Monorepo structure with Turborepo
2. ✅ Drizzle schema → auto-generated types
3. ✅ Effect.js for typed, composable APIs
4. ✅ Separate UI packages per platform
5. ✅ Shared domain models in web-domain
6. ✅ Docker Compose for local services
7. ✅ Biome for format/lint consistency
8. ✅ TypeScript strict mode everywhere
9. ✅ End-to-end type safety (DB→API→UI)
10. ✅ Rust for compute-intensive work
11. ✅ Deployment options: Docker, K8s, Serverless
12. ✅ OpenTelemetry for observability

---

## Using This With AI Assistants

**Good Prompt**:
> "Generate a project following the Cap architecture for a customer support platform. Replace 'Recording' with 'Ticket', 'Share' with 'Escalate', and 'Workspace' with 'Team'. Include database schema, API endpoints, and React components."

**Result**: Fully structured, type-safe codebase in 5 minutes.

**Iteration**: "Add a webhook system to notify external services when a ticket is escalated" → Agent follows the same patterns without additional instruction.

---

## Scaling Milestones

| Users | Changes |
|-------|---------|
| 1-100 | Single server, SQLite or single MySQL |
| 100-1k | Separate app/worker, Redis cache, CDN |
| 1k-10k | Database replicas, horizontal app scaling, analytics |
| 10k-100k | Kubernetes, distributed database, region replication |
| 100k+ | Multi-region, event sourcing, sharding |

---

## Common Patterns by Use Case

### SaaS Dashboard
- Multi-tenant workspaces (workspace_id on all tables)
- Subscription management (billing service)
- Analytics (Tinybird or PostHog)
- Audit logs (immutable events table)

### Content Management
- Version history (content_version table)
- Rich editor (block-based schema)
- Media processing (Rust crate for encoding)
- CDN caching headers

### Collaboration Tool
- Real-time sync (WebSocket + operational transforms)
- Conflict resolution (CRDT or last-write-wins)
- Permission matrix (RBAC in database)
- Activity timeline (immutable events)

### Mobile-First App
- Sync engine (local-first, eventual consistency)
- Push notifications (FCM/APNs)
- Offline support (SQLite + sync)
- Battery optimization (minimal polling)
