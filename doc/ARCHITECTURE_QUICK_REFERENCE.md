# Architecture Quick Reference Card

## 30-Second Overview

**Cap** is a full-stack application with desktop, web, mobile, and CLI interfaces sharing a single codebase. It uses:
- **Monorepo**: Turborepo for orchestration
- **Frontend**: React (web), Solid.js (desktop), React Native (mobile)
- **Backend**: Next.js with Effect.js typed APIs
- **Database**: MySQL with Drizzle ORM (auto-typed)
- **Processing**: Rust crates for performance-critical tasks
- **Deployment**: Docker Compose, Kubernetes, or Serverless

---

## Directory Map (2-Minute Read)

| Path | What | Tech | Why |
|------|------|------|-----|
| `apps/web` | Web app + API | Next.js + React | Dashboard & server routes |
| `apps/desktop` | Desktop app | Tauri + Solid.js | Native performance, offline |
| `apps/cli` | Command-line | Rust | Dev automation |
| `apps/mobile` | Mobile app | React Native | iOS/Android sharing |
| `packages/database` | ORM + schema | Drizzle | Type-safe SQL |
| `packages/web-backend` | Business logic | Effect.js | Typed, composable services |
| `packages/web-domain` | Shared types | TypeScript | Single source of truth |
| `packages/ui` | React components | React | Web + extensions |
| `packages/ui-solid` | Solid components | Solid.js | Desktop UI reuse |
| `crates/` | Rust modules | Rust | Recording, encoding, camera |

---

## The Core Pattern (Copy-Paste Template)

### 1. Schema
```typescript
// packages/database/schema.ts
export const items = mysqlTable("items", {
  id: varchar("id", { length: 128 }).primaryKey(),
  userId: varchar("user_id").references(() => users.id),
  title: varchar("title"),
  createdAt: timestamp("created_at").defaultNow(),
});
```

### 2. Service
```typescript
// packages/web-backend/items.ts
export const GetItem = Effect.gen(function* () {
  const db = yield* Database;
  const { id } = yield* Request;
  return yield* db.query.items.findById(id);
});
```

### 3. API Route
```typescript
// apps/web/app/api/items/[id].ts
export const handler = apiToHandler(
  ApiBuilder.get("/api/items/:id", GetItem)
);
```

### 4. Frontend
```typescript
// apps/web/components/ItemDetail.tsx
export function ItemDetail({ id }: { id: string }) {
  const { data } = useEffectQuery("getItem", { id });
  return <div>{data?.title}</div>;
}
```

**Result**: Types flow from DB → API → UI automatically. No manual updates.

---

## Command Cheat Sheet

```bash
# Setup (once)
pnpm install && pnpm env-setup && pnpm cap-setup

# Development (daily)
pnpm dev              # Everything
pnpm dev:web          # Just web
pnpm dev:desktop      # Just desktop

# Database (when schema changes)
pnpm db:generate      # Create migrations
pnpm db:push          # Apply them

# Before commit
pnpm lint --write     # Fix formatting
pnpm typecheck        # Type check
pnpm test             # Run tests

# Deploy
pnpm build            # Build all
docker compose up     # Local stack
```

---

## Adding a Feature (6 Steps)

### Feature: Add "Priority" field to items

**1. Schema** (`packages/database/schema.ts`)
```typescript
priority: varchar("priority", { enum: ["low", "medium", "high"] }).default("low"),
```

**2. Generate Types**
```bash
pnpm db:generate
```

**3. API Service** (`packages/web-backend/items.ts`)
```typescript
export const UpdateItemPriority = Effect.gen(function* () {
  const db = yield* Database;
  const { id, priority } = yield* Request;
  return yield* db.update(items).set({ priority });
});
```

**4. Expose Route** (`apps/web/app/api/items/[id].ts`)
```typescript
export const handler = apiToHandler(
  ApiBuilder.patch("/api/items/:id", UpdateItemPriority)
);
```

**5. UI Component** (`packages/ui/PrioritySelect.tsx`)
```typescript
export function PrioritySelect({ value, onChange }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="high">High</option>
    </select>
  );
}
```

**6. Use in Desktop + Web** (`packages/ui-solid/PrioritySelect.tsx`)
```typescript
// Same component, Solid.js version
```

---

## Type Safety Flow

```
Schema Definition
    ↓
pnpm db:generate  ← Auto-creates types
    ↓
Backend Service   ← Types inferred from schema
    ↓
API Route         ← Types inferred from service
    ↓
Frontend Query    ← Types inferred from API route
    ↓
UI Component      ← Full autocomplete, zero unknowns
```

**No manual type definitions ever needed.**

---

## Architecture Decision Records

| Decision | Why | Trade-off |
|----------|-----|-----------|
| Monorepo | Code sharing, single version | Complexity, slower CI |
| Drizzle + MySQL | Type safety, migrations | Need to learn ORM patterns |
| Effect.js | Typed, composable | Functional programming learning curve |
| Tauri + Solid | Native desktop, performance | Desktop-only ecosystem |
| Rust for processing | Speed, memory safety | Longer compile times |
| Docker Compose local dev | Realistic prod parity | Requires Docker |
| Turborepo | Parallel tasks, caching | Config complexity |

---

## Deployment Options

### Option 1: Docker Compose (1 command)
```bash
docker compose up -d
# Available at localhost:3000
```
**Good for**: Testing, self-hosting on single VPS

### Option 2: Kubernetes (production-ready)
```bash
helm install cap ./helm/cap
```
**Good for**: Scale to millions, multi-region, auto-recovery

### Option 3: Serverless (AWS Lambda)
```bash
sst deploy
```
**Good for**: Zero-ops, pay-per-request, global distribution

---

## Scaling Path

| Stage | When | Changes |
|-------|------|---------|
| Solo | <100 users | Single container, single DB |
| Growing | 100-1k | Redis cache, CDN, DB replicas |
| Popular | 1k-10k | Kubernetes, regional caching |
| Massive | 10k+ | Multi-region, database sharding |

---

## File Locations for Common Tasks

| Task | File | Pattern |
|------|------|---------|
| Add table | `packages/database/schema.ts` | `mysqlTable("name", { ... })` |
| Add API endpoint | `apps/web/app/api/[...].ts` | `apiToHandler(...)` |
| Add UI component | `packages/ui/Component.tsx` | React + Tailwind |
| Add desktop UI | `packages/ui-solid/Component.tsx` | Solid.js + Tailwind |
| Add utility | `packages/utils/lib.ts` | Export function |
| Add test | `*.test.ts` | Vitest + describe/test |
| Add Rust | `crates/[module]/src/lib.rs` | Rust std lib |
| Configure env | `packages/env/index.ts` | Zod schema |

---

## Testing Quick Start

```typescript
// Unit test (fast, isolated)
describe("calculatePrice", () => {
  test("applies discount", () => {
    expect(calculatePrice(100, 0.1)).toBe(90);
  });
});

// Integration test (real DB)
describe("createItem", () => {
  test("saves to database", async () => {
    const item = await db.items.create({ title: "Test" });
    expect(item.id).toBeDefined();
  });
});

// E2E test (full flow)
describe("user creates item", () => {
  test("item appears in list", async () => {
    await page.click("Create");
    await page.fill("Title", "Test");
    await page.click("Save");
    expect(page.url()).toContain("/items/");
  });
});
```

---

## Performance Checklist

- [ ] Database indexes on `userId`, `workspaceId`, common filters
- [ ] API pagination (limit/offset or cursor)
- [ ] Redis caching for repeated queries
- [ ] CDN for images, videos, static assets
- [ ] Connection pooling on database
- [ ] Code splitting in React/SolidStart
- [ ] Image optimization (next/image)
- [ ] Gzip compression in Rust crates

---

## Security Checklist

- [ ] Environment variables for secrets (`.env`, not in code)
- [ ] JWT validation on every API endpoint
- [ ] Database row-level security for workspace isolation
- [ ] Rate limiting on public endpoints
- [ ] HTTPS/TLS everywhere
- [ ] CORS configured correctly
- [ ] Input validation with Zod
- [ ] SQL injection prevention (Drizzle handles it)

---

## IDE Setup (VS Code)

```json
{
  "extensions": [
    "biomejs.biome",
    "rust-lang.rust-analyzer",
    "dbaeumer.vscode-eslint"
  ],
  "settings": {
    "editor.formatOnSave": true,
    "[typescript]": { "editor.defaultFormatter": "biomejs.biome" },
    "[rust]": { "editor.defaultFormatter": "rust-lang.rust-analyzer" }
  }
}
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Types not generating | `pnpm db:generate && pnpm typecheck` |
| API changes not reflecting in UI | Clear `.next` folder, restart dev server |
| Desktop app won't start | Check `pnpm dev:desktop` logs, delete `src-tauri/target` |
| Database connection error | Verify `DATABASE_URL` in `.env`, check Docker |
| Port already in use | `lsof -i :3000` to find, `kill -9 <PID>` to stop |

---

## Learning Path

1. **Day 1**: Understand monorepo structure, run `pnpm dev`
2. **Day 2**: Modify schema, run `pnpm db:generate`, see types update
3. **Day 3**: Create API endpoint, use Effect.js pattern
4. **Day 4**: Add React component, connect to API
5. **Day 5**: Add Solid.js component for desktop
6. **Day 6**: Deploy locally with Docker Compose
7. **Day 7**: Deploy to production (Vercel/Railway/K8s)

---

## Key Insights

1. **Type Generation is Automatic**: Schema changes → types cascade everywhere
2. **Code Sharing Maximized**: 80% of logic shared across platforms
3. **Effect.js Eliminates Boilerplate**: Typed, composable, reusable services
4. **Rust for Performance**: Hot paths in native code, 10-100x faster
5. **Local-First Dev Experience**: `docker compose up` mimics production
6. **Self-Hosting First**: Deploy anywhere—cloud, on-prem, edge
7. **Observable by Default**: Logs, traces, metrics built-in

---

## Resources

- **Monorepo**: [Turborepo Docs](https://turbo.build)
- **Desktop**: [Tauri Docs](https://tauri.app)
- **Backend**: [Effect.js Docs](https://effect.website)
- **Database**: [Drizzle Docs](https://orm.drizzle.team)
- **Framework**: [Next.js Docs](https://nextjs.org)
- **Styling**: [Tailwind Docs](https://tailwindcss.com)

---

## One-Liner Explanations

- **Turborepo**: Runs build tasks in parallel, caches results
- **Drizzle**: Write schema in TypeScript, generates SQL migrations
- **Effect**: Compose business logic like LEGO blocks, automatic error handling
- **Tauri**: Runs Rust backend inside desktop app, web frontend
- **Next.js**: React framework that handles routing, SSR, API routes
- **Solid.js**: Lightweight React alternative, great for desktop
- **Zod**: Runtime validation, TypeScript types inferred

---

## Commit Message Template

```
feat(web): add priority field to items

- Add priority column to items table
- Create UpdatePriority API endpoint
- Add PrioritySelect component for React and Solid
- Update item detail view to show priority

Closes #123
```

**Format**: `type(scope): description`
- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code cleanup
- `docs`: Documentation
- `test`: Tests only

---

## Emergency Reference

**Everything breaks?** Start here:
```bash
rm -rf .turbo node_modules crates/*/target apps/web/.next
pnpm install
pnpm env-setup
pnpm cap-setup
pnpm db:generate
pnpm dev
```

**Still broken?** Check:
1. Node 20+, pnpm 10.5.2, Rust 1.88+
2. Docker running (`docker ps`)
3. `.env` file exists with `DATABASE_URL`
4. Port 3000/5173 not in use

---

## Print This Card

Save as PDF for desk reference. Updates quarterly.

**Current Version**: Cap 2024 Q4
**Last Updated**: Aug 6, 2026
**Contact**: See CONTRIBUTING.md for support
