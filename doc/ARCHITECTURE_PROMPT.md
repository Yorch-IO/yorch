# Multi-Platform Application Architecture Prompt

## Executive Summary

This prompt describes a scalable, modular architecture for building full-featured applications that run across desktop, web, mobile, and CLI environments. The architecture prioritizes code sharing, type safety, media processing capabilities, and self-hosting support.

---

## Core Architecture Principles

1. **Monorepo Structure**: Single repository with workspace management (Turborepo)
2. **Polyglot Stack**: TypeScript/JavaScript for UX layers, Rust for performance-critical operations
3. **Type Safety**: End-to-end typing from database schema to frontend components
4. **Separation of Concerns**: Clear boundaries between platforms, services, and domain logic
5. **Self-Hosting First**: Support for both cloud-hosted and self-hosted deployments
6. **Pluggable Storage**: Flexible storage backends (cloud or self-managed)

---

## Repository Structure

### High-Level Organization

```
├── apps/                    # Platform-specific applications
│   ├── desktop/             # Desktop application (cross-platform: macOS, Windows)
│   ├── web/                 # Web application and API server
│   ├── cli/                 # Command-line interface
│   ├── mobile/              # Mobile application (iOS, Android)
│   ├── chrome-extension/    # Browser extension
│   ├── media-server/        # Media processing microservice
│   ├── discord-bot/         # Third-party integration bot
│   └── storybook/           # Component documentation
│
├── packages/                # Shared libraries and services
│   ├── database/            # Data layer with ORM and migrations
│   ├── web-backend/         # Server-side business logic
│   ├── web-api-contract/    # API contract definitions
│   ├── web-api-contract-effect/  # Typed Effect.js API builders
│   ├── web-domain/          # Shared domain models and types
│   ├── ui/                  # React component library
│   ├── ui-solid/            # Solid.js component library
│   ├── utils/               # Utility functions and helpers
│   ├── env/                 # Environment validation and config
│   ├── config/              # Shared configuration
│   ├── sdk-embed/           # Embeddable SDK for third parties
│   ├── sdk-recorder/        # Recording SDK for developers
│   ├── s3/                  # S3-compatible storage client
│   ├── recorder-core/       # Core recording logic
│   ├── local-docker/        # Local development services
│   ├── tsconfig/            # Shared TypeScript configuration
│   └── web-api-contract-effect/  # Effect-based typed APIs
│
├── crates/                  # Rust-specific modules
│   ├── api/                 # API layer (platform-specific service)
│   ├── recording/           # Recording engine
│   ├── encoding/            # Video encoding and compression
│   ├── camera*/             # Platform-specific camera capture
│   ├── audio/               # Audio processing
│   ├── cursor-capture/      # Cursor detection and rendering
│   ├── editor/              # Post-processing effects
│   ├── export/              # File export functionality
│   ├── muxing/              # Media container handling
│   └── automation/          # Automated testing framework
│
├── scripts/                 # Development and deployment scripts
│   ├── setup.js             # Initial project setup
│   ├── analytics/           # Analytics pipeline tooling
│   ├── build-*              # Build automation
│   └── env-cli.js           # Environment configuration CLI
│
├── infra/                   # Infrastructure as Code
│   └── sst.config.ts        # Serverless infrastructure config
│
└── [Config Files]
    ├── Cargo.toml           # Rust workspace manifest
    ├── Cargo.lock           # Rust dependency lock
    ├── package.json         # Node.js workspace manifest
    ├── pnpm-workspace.yaml  # pnpm workspace configuration
    ├── turbo.json           # Turborepo configuration
    ├── tsconfig.json        # TypeScript base configuration
    ├── biome.json           # Code formatting and linting
    ├── docker-compose.yml   # Local development environment
    └── AGENTS.md            # AI agent coding guidelines
```

---

## Platform Applications

### Desktop Application (`apps/desktop`)

**Tech Stack**: Tauri v2 + SolidStart + Rust backend

**Purpose**: Native application for recording, editing, and exporting content

**Key Features**:
- Native system integration (camera, microphone, screen capture)
- Local-first recording with optional upload
- Real-time streaming to web server
- Offline capability with local storage
- Platform-specific performance optimizations

**Architecture**:
```
apps/desktop/
├── src/                     # SolidStart frontend
│   ├── components/          # UI components
│   ├── routes/              # Page routes
│   ├── utils/               # Utilities
│   └── ...
├── src-tauri/               # Rust backend
│   ├── src/                 # Rust source
│   ├── icons/               # App icons
│   └── tauri.conf.json      # Tauri configuration
└── package.json
```

### Web Application (`apps/web`)

**Tech Stack**: Next.js + React + Effect.js + TypeScript

**Purpose**: Dashboard, API server, authentication, and sharing interface

**Key Features**:
- User authentication and authorization
- REST/GraphQL API with typed contracts
- Dashboard for managing content
- Share pages with viewer analytics
- Admin panel for multi-tenant management
- Email notifications and webhooks

**Architecture**:
```
apps/web/
├── app/                     # Next.js app directory
│   ├── api/                 # API routes (Effect.js pattern)
│   │   ├── auth/            # Authentication endpoints
│   │   ├── dashboard/       # User dashboard endpoints
│   │   ├── analytics/       # Telemetry endpoints
│   │   ├── integrations/    # Third-party integrations
│   │   └── ...
│   ├── dashboard/           # Protected dashboard pages
│   ├── [id]/                # Public share pages
│   └── ...
├── components/              # React components
├── hooks/                   # Custom React hooks
├── lib/                     # Utilities (Effect runtime, etc.)
├── types/                   # TypeScript types
├── actions/                 # Server actions
└── package.json
```

### CLI Application (`apps/cli`)

**Tech Stack**: Rust CLI with Clap

**Purpose**: Command-line interface for developers and automation

**Key Features**:
- Recording programmatically
- Batch export operations
- Configuration management
- Integration with CI/CD pipelines

### Media Server (`apps/media-server`)

**Tech Stack**: Rust with streaming capabilities

**Purpose**: Handle media processing tasks asynchronously

**Key Features**:
- Video transcoding
- Thumbnail generation
- Format conversion
- Stream processing

### Mobile Application (`apps/mobile`)

**Tech Stack**: React Native / Expo

**Purpose**: Mobile viewing and sharing interface

### Chrome Extension (`apps/chrome-extension`)

**Tech Stack**: TypeScript + Chrome APIs

**Purpose**: Quick recording from browser

### Discord Bot (`apps/discord-bot`)

**Tech Stack**: Discord.js + TypeScript

**Purpose**: Discord integration for sharing and collaboration

---

## Data Layer (`packages/database`)

### Technology Stack
- **ORM**: Drizzle ORM
- **Database**: MySQL (with support for PlanetScale)
- **Migration**: Drizzle Kit
- **Query**: Type-safe SQL generation

### Schema Organization
```typescript
// All tables defined with full type safety
export const users = mysqlTable("users", {
  id: varchar("id", { length: 128 }).primaryKey(),
  email: varchar("email", { length: 256 }).notNull().unique(),
  name: varchar("name", { length: 256 }),
  createdAt: timestamp("created_at").defaultNow(),
});

export const recordings = mysqlTable("recordings", {
  id: varchar("id", { length: 128 }).primaryKey(),
  userId: varchar("user_id", { length: 128 })
    .references(() => users.id),
  title: varchar("title", { length: 256 }),
  // ... more fields
});
```

### Database Workflow
1. Schema defined in TypeScript
2. Generate migrations: `pnpm db:generate`
3. Push to database: `pnpm db:push`
4. Generate types automatically: `db:generate`
5. Use typed queries in application code

---

## Backend Service Layer (`packages/web-backend` + `packages/web-api-contract-effect`)

### Effect.js Pattern

All API endpoints follow the Effect.js pattern for:
- **Type Safety**: Full type inference from definition to client
- **Error Handling**: Typed error variants
- **Dependency Injection**: Service layer management
- **Composability**: Reusable service definitions

### API Route Pattern
```typescript
// api/dashboard/recordings.ts
export const handler = apiToHandler(
  ApiBuilder.group("/api/dashboard/recordings", (group) =>
    group
      .get("list", ListRecordingsEndpoint)
      .post("create", CreateRecordingEndpoint)
      .get("/:id", GetRecordingEndpoint)
  ).pipe(Layer.provide(ApiLive))
);
```

### Service Definition Pattern
```typescript
// Define once, use everywhere (server + client)
export class RecordingsApi extends TaggedClass("RecordingsApi")({
  list: Function.pipe(
    Effect.promise(() => api.recordings.list()),
    Effect.catchTag("NotFound", () => Effect.succeed([]))
  ),
  create: (data) => api.recordings.create(data),
});
```

---

## Type Safety & API Contracts

### Contract Definition (`packages/web-api-contract`)

Defines shared API types between frontend and backend:
```typescript
// contracts.ts
export const RecordingContract = {
  id: z.string().cuid(),
  title: z.string().min(1),
  duration: z.number().int().positive(),
  createdAt: z.date(),
};
```

### Type Generation
- Database schema → TypeScript types (automatic)
- API contracts → Client SDK (generated)
- OpenAPI/REST → Type-safe fetch (automatic)

---

## Shared Libraries

### UI Libraries

#### React Components (`packages/ui`)
- Reusable React components
- Used by web and chrome extension
- Shadcn-based component system
- Tailwind CSS styling

#### Solid Components (`packages/ui-solid`)
- Solid.js components
- Used by desktop application (SolidStart)
- Same design system as React components
- Performance optimized for desktop

### Domain Models (`packages/web-domain`)

Shared types used across all platforms:
```typescript
export interface User {
  id: string;
  email: string;
  name: string;
  role: "user" | "admin";
}

export interface Recording {
  id: string;
  userId: string;
  title: string;
  duration: number;
  createdAt: Date;
}
```

### Utilities (`packages/utils`)

Common functions and helpers:
- Date/time utilities
- String manipulation
- Array operations
- Validation helpers
- Error handling utilities

### Environment Configuration (`packages/env`)

Typed environment validation:
```typescript
export const env = z.object({
  DATABASE_URL: z.string().url(),
  JWT_SECRET: z.string().min(32),
  S3_BUCKET: z.string(),
  NODE_ENV: z.enum(["development", "production"]),
}).parse(process.env);
```

---

## Rust Core Crates (`crates/`)

### Recording & Capture Pipeline

```
recording-engine
├── capture-screen
├── capture-camera
├── capture-audio
└── realtime-encoding

encoding
├── codec-h264
├── codec-vp9
├── codec-av1
└── codec-prores

audio-processing
├── noise-reduction
├── audio-mixing
└── level-normalization

export-formats
├── mp4
├── webm
├── mov
└── custom-containers
```

### Platform-Specific Modules

**macOS**:
- `camera-avfoundation` - AVFoundation camera integration
- `cursor-capture-macos` - Native cursor rendering

**Windows**:
- `camera-directshow` - DirectShow camera integration
- `camera-mediafoundation` - Media Foundation
- `d3d-adapter` - Direct3D rendering

---

## Development Environment

### Docker Compose Setup

```yaml
services:
  mysql:
    image: mysql:8
    environment:
      MYSQL_ROOT_PASSWORD: root
    volumes:
      - mysql_data:/var/lib/mysql

  minio:
    image: minio/minio
    ports:
      - "9000:9000"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin

  redis:
    image: redis:7-alpine
```

### Development Scripts

```bash
# Initial setup
pnpm install
pnpm env-setup        # Configure environment variables
pnpm cap-setup        # Install OS dependencies, start services

# Development
pnpm dev              # Start all services
pnpm dev:web          # Web only
pnpm dev:desktop      # Desktop only

# Database
pnpm db:generate      # Generate types from schema
pnpm db:push          # Apply migrations
pnpm db:studio        # Open visual editor

# Quality checks
pnpm lint             # Run Biome linter
pnpm format           # Format with Biome
pnpm typecheck        # TypeScript type checking

# Building
pnpm build            # Full build with Turbo
pnpm tauri:build      # Desktop release build
```

---

## Code Quality & Standards

### Formatting & Linting (Biome)
- Tabs for indentation
- Double quotes for strings
- Automatic import organization
- No unused variables or imports
- No implicit `any` types

### Rust Standards (Clippy)
- No `dbg!()` macros (use `tracing::debug!`)
- No unused futures
- Safe time operations
- Nested if consolidation
- Proper error handling patterns

### TypeScript Standards
- Strict mode enabled
- No `@ts-ignore` / `@ts-expect-error` without reason
- Prefer `unknown` over `any`
- Exhaustive pattern matching

### Comments Policy
- **Default**: No comments
- **Add only if**: Non-obvious context, workaround explanation, upstream bug reference
- **Avoid**: Narrating code, restating types, JSDoc paraphrasing

---

## Deployment Architecture

### Self-Hosting Options

#### Docker Compose (Single Server)
```bash
docker compose up -d
# Available at http://localhost:3000
```

#### Kubernetes (Distributed)
```yaml
services:
  - cap-web (Next.js + API)
  - cap-media-server (Processing)
  - mysql (Database)
  - minio (Storage)
```

#### Serverless (AWS Lambda)
- Infrastructure as Code with SST
- Located in `infra/sst.config.ts`
- Automatic environment provisioning

### Configuration

**Environment Variables**:
```env
# Database
DATABASE_URL=mysql://user:password@host:3306/cap

# Storage
S3_ENDPOINT=https://s3.amazonaws.com
S3_BUCKET=cap-storage
S3_REGION=us-east-1
S3_PUBLIC_URL=https://storage.yourdomain.com

# Application
CAP_URL=https://cap.yourdomain.com
NODE_ENV=production
JWT_SECRET=<32+ character secret>

# Services (optional)
SMTP_FROM=noreply@cap.yourdomain.com
SMTP_HOST=mail.provider.com
STRIPE_SECRET_KEY=sk_live_...
```

---

## Scaling Patterns

### Horizontal Scaling

**Read Scaling**:
- Multiple Next.js instances behind load balancer
- Read replicas for database queries
- CDN for static assets and video delivery

**Write Scaling**:
- Database write leader + read replicas
- Message queue (Redis/RabbitMQ) for async tasks
- Sharded media server instances

**Media Processing**:
- Dedicated media server fleet
- Job queue (Bull/BullMQ) for encoding tasks
- Distributed transcoding with status tracking

### Caching Strategy

- **CDN**: Videos, thumbnails, static assets
- **Redis**: User sessions, API responses, rate limits
- **Browser**: Service workers for offline support
- **Database**: Query result caching

---

## Analytics & Monitoring

### Telemetry Collection
- Tinybird datasources for aggregated analytics
- PostHog for user behavior tracking
- Custom event schema for domain-specific metrics

### Observability Stack
```
Application → OpenTelemetry → Collector → 
  → Prometheus (metrics) 
  → Jaeger (traces) 
  → Loki (logs)
```

---

## Security Considerations

### Authentication & Authorization
- **Desktop**: JWT tokens stored securely
- **Web**: NextAuth.js session management
- **API**: Bearer token validation
- **Mobile**: OAuth with redirect scheme

### Data Protection
- Encryption at rest (S3 server-side)
- Encryption in transit (HTTPS/TLS)
- Row-level security in database
- API rate limiting and DDoS protection

### Self-Hosting Security
- Network isolation (VPC)
- Secrets management (not in .env)
- Regular backups with encryption
- Security scanning in CI/CD

---

## Testing Strategy

### Unit Tests
- Location: `*.test.ts` / `*.test.tsx` next to source
- Framework: Vitest (TypeScript), `cargo test` (Rust)
- Coverage: Business logic and utilities

### Integration Tests
- Database: Real MySQL instance
- API: End-to-end endpoint testing
- Services: Multi-service interaction

### End-to-End Tests
- Desktop: Tauri testing framework
- Web: Playwright or Cypress
- Mobile: Detox

### Continuous Integration
- Linting on every commit
- Type checking on PR
- Tests on all platforms
- Build verification

---

## Extension Points

### Adding New Platforms

1. Create new app directory: `apps/[platform]`
2. Import shared packages from `packages/`
3. Implement platform-specific UI
4. Use Effect.js for backend integration
5. Add to `docker-compose.yml` if service

### Adding New API Endpoints

1. Define contract in `packages/web-api-contract`
2. Implement service in `packages/web-backend`
3. Create API route in `apps/web/app/api`
4. Expose in `packages/web-api-contract-effect`
5. Generate client types

### Adding New Domain Models

1. Define schema in `packages/database`
2. Run `pnpm db:generate`
3. Add types to `packages/web-domain`
4. Create API endpoints as needed
5. Update UI components

---

## Example: Adding Recording Export Feature

### 1. Database Schema (`packages/database`)
```typescript
export const exportJobs = mysqlTable("export_jobs", {
  id: varchar("id", { length: 128 }).primaryKey(),
  recordingId: varchar("recording_id"),
  format: varchar("format"), // "mp4", "webm", "mov"
  status: varchar("status"), // "pending", "processing", "complete"
  createdAt: timestamp("created_at").defaultNow(),
});
```

### 2. API Contract (`packages/web-api-contract`)
```typescript
export const ExportRecordingRequest = z.object({
  recordingId: z.string().cuid(),
  format: z.enum(["mp4", "webm", "mov"]),
});
```

### 3. Backend Service (`packages/web-backend`)
```typescript
export const ExportRecording = Effect.gen(function* () {
  const db = yield* Database;
  const jobs = yield* JobQueue;
  const request = yield* Context;
  
  const job = yield* db.insert(exportJobs).values({
    recordingId: request.recordingId,
    format: request.format,
  });
  
  yield* jobs.enqueue("export", job);
});
```

### 4. API Route (`apps/web/app/api/recordings/export.ts`)
```typescript
export const handler = apiToHandler(
  ApiBuilder.post("/api/recordings/export", ExportRecording)
    .pipe(Layer.provide(ExportRecordingLive))
);
```

### 5. Desktop Client (`apps/desktop/src`)
```typescript
export async function exportRecording(recordingId: string, format: "mp4" | "webm" | "mov") {
  const response = await fetch("/api/recordings/export", {
    method: "POST",
    body: JSON.stringify({ recordingId, format }),
  });
  return response.json();
}
```

---

## Key Takeaways for AI-Assisted Development

1. **Monorepo Organization**: Use Turborepo for task coordination
2. **Type-First Design**: Let types guide API contracts
3. **Layered Architecture**: Separate concerns (UI, API, Domain, Data)
4. **Code Sharing**: Maximize package reuse across platforms
5. **Self-Hosting**: Design with deployment flexibility
6. **Performance**: Use Rust for CPU-bound operations
7. **Developer Experience**: Automated formatting, linting, type generation
8. **Scalability**: Design for horizontal scaling from day one
9. **Testing**: Test at integration level, not just unit
10. **Analytics**: Build observability from the start

---

## Command Reference

```bash
# Setup & Installation
pnpm install                    # Install all dependencies
pnpm env-setup                  # Configure environment
pnpm cap-setup                  # Complete setup

# Development
pnpm dev                        # Start all dev servers
pnpm dev:web                    # Web only
pnpm dev:desktop                # Desktop only
pnpm dev:mobile                 # Mobile only

# Database
pnpm db:generate                # Generate schema types
pnpm db:push                    # Apply migrations
pnpm db:studio                  # Visual DB editor
pnpm db:drop                    # Reset database

# Building
pnpm build                      # Full workspace build
pnpm tauri:build                # Desktop release

# Quality
pnpm lint                       # Biome linting
pnpm format                     # Biome formatting
pnpm typecheck                  # TypeScript check

# Testing
pnpm test                       # All tests
cargo test -p <crate>          # Rust tests

# Deployment
docker compose up -d            # Start local stack
docker compose down             # Stop services
```

---

## Further Reading

- **Turborepo Docs**: Monorepo optimization and task scheduling
- **Tauri Docs**: Desktop app framework capabilities
- **Next.js Docs**: Modern React framework features
- **Drizzle Docs**: Type-safe ORM patterns
- **Effect.js Docs**: Functional programming patterns
- **Rust Book**: Systems programming fundamentals
