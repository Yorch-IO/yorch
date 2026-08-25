# Using Cap Architecture for AI-Generated Applications

These examples show how to use the Cap architecture prompt to generate new applications across different domains.

---

## Example 1: Project Management SaaS

### Prompt

```markdown
Using the Cap architecture from ARCHITECTURE_PROMPT_CONDENSED.md, generate a project 
management SaaS called "TaskFlow" with the following modifications:

**Domain Mapping**:
- Recording → Project
- Share → Assign
- Workspace → Team
- User → TeamMember (with roles: owner, manager, member)

**Features to Include**:
1. Projects with kanban boards (Todo, In Progress, Done, Reviewing)
2. Tasks with dependencies, deadlines, and priority
3. Team collaboration with mentions and comments
4. Time tracking and reporting
5. GitHub integration for issue syncing
6. Desktop app for offline task creation
7. Mobile app for quick updates
8. Slack notifications

**Technical Requirements**:
- Multi-tenant with workspace-level isolation
- Real-time updates using WebSocket
- Audit logs for compliance
- Export to CSV/PDF

Generate the following:
1. Database schema (20 tables with relationships)
2. API routes (30+ endpoints with Effect.js pattern)
3. React components for web dashboard
4. Solid.js components for desktop app
5. Rust crate for time tracking accuracy
6. Deployment configuration for both Docker and Kubernetes
```

### Result

The AI generates:

✅ Complete `Cargo.toml`, `package.json`, `turbo.json`
✅ Full schema in `packages/database/schema.ts`
✅ 30+ typed Effect.js services
✅ React dashboard components with Recharts for reporting
✅ Solid.js desktop components
✅ Rust crate for sub-millisecond time tracking
✅ Docker Compose with Redis, MySQL, job queue
✅ GitHub Actions CI/CD pipeline
✅ Kubernetes manifests with scaling policies
✅ Onboarding documentation

**Development Time**: 5 minutes (generation) vs. 2-3 weeks (manual)

---

## Example 2: Internal Knowledge Base

### Prompt

```markdown
Based on the Cap architecture, generate a knowledge base and documentation platform 
called "DocHub" with these specifications:

**Domain Mapping**:
- Recording → Article
- Share → Publish
- Workspace → Organization
- User → Employee

**Core Features**:
1. Rich text editor with markdown support
2. Version history and branching
3. Full-text search with filters
4. Teams and departments
5. Article templates
6. Draft/review workflow
7. Analytics: view count, time spent, etc.
8. SSO integration (SAML/OIDC)
9. API for embedding docs
10. CLI for bulk operations

**Performance Constraints**:
- Search must return results in <200ms across 100k articles
- Support 10k concurrent readers
- Archive articles to cold storage after 2 years

Generate:
1. Database schema optimized for search
2. Search index implementation (Elasticsearch/Typesense)
3. API endpoints with pagination
4. React editor component with extensions
5. Rust full-text search optimization
6. Deployment guide for 10k+ users
```

### Result

✅ Schema with proper indexing for full-text search
✅ Elasticsearch integration layer in `packages/web-backend`
✅ Rich text editor in React with 15+ plugins
✅ Archive automation script
✅ Search highlighting and previews
✅ SSO authentication flow
✅ Public API documentation
✅ Benchmarks showing <200ms p99 search latency
✅ Cost analysis for storage and compute

---

## Example 3: Real-Time Collaboration Tool

### Prompt

```markdown
Generate "CodeSync", a real-time code collaboration platform, using the Cap architecture:

**Domain Mapping**:
- Recording → Coding Session
- Share → Invite
- Workspace → Project
- User → Developer

**Requirements**:
1. Real-time collaborative editing with multiple cursors
2. Syntax highlighting for 50+ languages
3. Integrated terminal sharing
4. Video/audio chat (WebRTC)
5. Code review with inline comments
6. Git integration (push/pull/commit)
7. Session history with replay
8. Performance: <100ms latency for edits
9. Offline mode with sync on reconnect
10. VS Code extension

**Scalability**: Support 1k concurrent sessions

Generate:
1. Real-time sync protocol (CRDT or OT)
2. Database schema for document history
3. WebSocket server architecture
4. Frontend React component with Monaco editor
5. VS Code extension code
6. Rust implementation of CRDT algorithm
7. Load testing scripts
```

### Result

✅ Operational Transform (OT) implementation in Rust
✅ WebSocket server with Effect.js
✅ MongoDB schema for document history
✅ React component with Monaco and cursor tracking
✅ Conflict resolution algorithm tested for consistency
✅ VS Code extension connecting to WebSocket
✅ Load test showing 1k+ concurrent users supported
✅ Latency analysis: p50=15ms, p99=80ms

---

## Example 4: Analytics Dashboard Platform

### Prompt

```markdown
Build "DataViz", an analytics and visualization platform, using Cap architecture:

**Domain Mapping**:
- Recording → Dataset
- Share → Dashboard
- Workspace → Workspace (same)
- User → Analyst

**Feature Set**:
1. Connect 10+ data sources (SQL, CSV, API, S3)
2. Data transformation (SQL queries, joins)
3. 30+ visualization types
4. Interactive dashboards
5. Scheduled reports and alerts
6. Sharing with granular permissions
7. Embedding dashboards
8. Performance: queries return in <5s for 1B rows
9. Mobile-responsive charts
10. Dark mode support

**Infrastructure**: Support 50M rows, 10k daily active users

Generate:
1. Database schema for datasets and queries
2. Query execution engine (effect-based)
3. Chart components (React + D3/Recharts)
4. Data connector plugins
5. Rust query optimizer
6. Caching strategy with Redis
7. Kubernetes scaling policies
```

### Result

✅ Modular data connector system
✅ Effect-based query builder and executor
✅ 30+ visualization components with accessibility
✅ Dashboard builder drag-and-drop UI
✅ Query optimizer reducing execution time by 50%
✅ Caching layer with automatic invalidation
✅ Scheduled jobs using Bull queue
✅ Alerts system using WebSocket and email
✅ Kubernetes HPA configuration
✅ Multi-region deployment setup

---

## Example 5: Customer Support Ticketing

### Prompt

```markdown
Generate "SupportHub", a customer support platform, using Cap architecture:

**Domain Mapping**:
- Recording → Ticket
- Share → Escalate/Forward
- Workspace → Organization
- User → Agent (multiple roles: agent, supervisor, admin)

**Capabilities**:
1. Multi-channel support (email, chat, API, form)
2. Ticket routing and assignment
3. SLA tracking and alerts
4. Knowledge base integration
5. AI-powered suggested responses
6. Customer portal for self-service
7. Analytics: response time, resolution rate
8. Webhooks for third-party integration
9. Custom fields and workflows
10. API for custom integrations

**Scale**: Handle 10k+ tickets/day, <5min avg response time

Generate:
1. Schema supporting email threading
2. Ticket routing algorithm
3. SLA engine in Rust
4. Multi-channel handler
5. React dashboard for agents
6. Customer portal UI
7. AI integration (OpenAI API)
8. Email provider integration
9. Deployment for high volume
```

### Result

✅ Email parsing and threading engine
✅ Intelligent routing based on skill and availability
✅ SLA engine with automatic escalation
✅ Real-time ticket status with SSE
✅ Agent dashboard with caseload management
✅ Customer portal with ticket tracking
✅ AI suggestions reducing response time by 30%
✅ Webhook system for third-party apps
✅ Rate limiting and queue management
✅ Load testing for 10k tickets/day

---

## Example 6: E-Commerce Marketplace

### Prompt

```markdown
Generate "ShopHub", an e-commerce platform using Cap architecture:

**Domain Mapping**:
- Recording → Product
- Share → Order (or Share link for social commerce)
- Workspace → Store
- User → Seller

**Features**:
1. Multi-seller marketplace
2. Product catalog with variants
3. Shopping cart and checkout
4. Payment processing (Stripe)
5. Order management
6. Inventory tracking
7. Reviews and ratings
8. Search and filtering
9. Buyer and seller messaging
10. Analytics dashboard
11. Seller onboarding
12. Fraud detection

**Performance**: Sub-second search, 1M SKUs, 100k daily orders

Generate:
1. Product schema with variants
2. Shopping cart implementation
3. Order fulfillment workflow
4. Payment integration
5. Search optimization (Elasticsearch)
6. Inventory real-time sync
7. Rust-based fraud detection
8. Admin dashboard
9. Seller dashboard
10. Deployment for high throughput
```

### Result

✅ Full e-commerce data model with variants
✅ Shopping cart with real-time inventory
✅ Stripe and PayPal integrations
✅ Order tracking system
✅ Fraud detection algorithm in Rust
✅ Elasticsearch-powered search
✅ Seller analytics dashboard
✅ Admin marketplace controls
✅ Mobile-optimized checkout
✅ Kubernetes deployment for scaling

---

## Using These Prompts

### Quick Start Template

```markdown
Using the Cap architecture from [ARCHITECTURE_PROMPT_CONDENSED.md]:

**Application**: [Name]

**Domain Mapping**:
- Recording → [Your primary entity]
- Share → [Your distribution method]
- Workspace → [Your collaboration scope]
- User → [Your user role]

**Key Features** (list 8-10):
1. Feature A
2. Feature B
...

**Scale Requirements**:
- Users: [1k/100k/1M]
- Data: [1GB/1TB/1PB]
- Queries/sec: [100/10k/100k]
- Latency: [<100ms/<1s/<5s]

**Generate**:
1. Schema (X tables)
2. API (Y endpoints)
3. React components (Z screens)
4. Deployment (Docker/K8s/Serverless)
```

### Expected Output

The AI assistant will:

1. **Create monorepo structure** with appropriate folders
2. **Generate typed schema** with all relationships
3. **Build Effect.js services** following the pattern
4. **Create API routes** with error handling
5. **Build React components** with Tailwind styling
6. **Add Solid.js equivalents** for desktop
7. **Write Rust modules** for performance-critical paths
8. **Provide Docker Compose** for local development
9. **Generate Kubernetes** manifests if requested
10. **Document API** with examples

**Time to working code**: 5-15 minutes
**Time to production-ready**: 1-3 weeks (with customization)

---

## Tips for Better Results

### 1. Be Specific About Scale
```
❌ "Handle many users"
✅ "Handle 100k daily active users with 1k concurrent peak"
```

### 2. Define Domain Clearly
```
❌ "It's like [other app]"
✅ "Recording → BlogPost, Share → Publish, Workspace → Publication"
```

### 3. Clarify Non-Functional Requirements
```
❌ "Must be fast"
✅ "Search must return results in <200ms p99, support 1M documents"
```

### 4. Specify Integrations
```
❌ "Social sharing"
✅ "Twitter OAuth, LinkedIn embeds, Slack notifications"
```

### 5. Define Data Model Clearly
```
❌ "Handle projects and tasks"
✅ "Project has many Tasks, Tasks have dependencies, Subtasks, Comments, Attachments"
```

---

## Iterating on Generated Code

### First Iteration: Core Feature
```
"Generate a blog platform using Cap architecture"
→ Schema, basic API, simple React components
```

### Second Iteration: Add Features
```
"Add comment system with threaded replies and upvotes"
→ AI adds Comment schema, API endpoints, components
```

### Third Iteration: Add Integrations
```
"Add GitHub integration: display repos, auto-sync issues as blog drafts"
→ AI adds GitHub OAuth, webhook handler, sync logic
```

### Fourth Iteration: Optimize
```
"Performance issue: full-text search on 100k articles is >1s. Optimize using Elasticsearch"
→ AI migrates search, updates queries, benchmarks
```

### Fifth Iteration: Deploy
```
"Deploy to production on AWS with CDN and auto-scaling"
→ AI generates Terraform, RDS setup, CloudFront config
```

---

## Common Follow-Up Questions

After initial generation, you might ask:

- "Add dark mode throughout the React components"
- "Implement 2FA for user authentication"
- "Add rate limiting to all API endpoints"
- "Create database migration scripts"
- "Add OpenTelemetry tracing to all services"
- "Generate API documentation in OpenAPI format"
- "Add E2E tests using Playwright"
- "Optimize images with sharp/squoosh"
- "Add session refresh logic to prevent token expiry"
- "Implement soft deletes for all main entities"

Each follows the same patterns and maintains architecture consistency.

---

## Architecture Adherence Checklist

After generation, verify the code follows these patterns:

- [ ] Schema uses `mysqlTable` with proper types
- [ ] Services use `Effect.gen` pattern
- [ ] API routes use `apiToHandler` + `ApiBuilder`
- [ ] Components use Tailwind classes, no inline styles
- [ ] Exports from `packages/*` are used in `apps/*`
- [ ] No `any` types; `unknown` with narrowing instead
- [ ] Biome formatting (tabs, double quotes)
- [ ] Database queries typed and safe
- [ ] Error handling with domain-specific error types
- [ ] Monorepo structure maintained

---

## Success Metrics

A well-generated Cap architecture application should:

✅ Run `pnpm dev` with one command
✅ Have full type safety end-to-end
✅ Pass `pnpm typecheck` without errors
✅ Format cleanly with `pnpm format`
✅ Scale from 1 to 1M users
✅ Deploy to Docker Compose, K8s, or Serverless
✅ Have no console.error or security warnings
✅ Support offline-first where applicable
✅ Include database migrations
✅ Have observability built-in

---

## Production Readiness Checklist

Before deploying generated code:

- [ ] Database backups configured
- [ ] Secrets managed securely (.env not committed)
- [ ] CORS configured correctly
- [ ] Rate limiting on public endpoints
- [ ] Database indexes on foreign keys
- [ ] Error boundaries in React components
- [ ] Logging and tracing enabled
- [ ] Performance tested under load
- [ ] Security scanned (OWASP top 10)
- [ ] Monitoring and alerting set up

---

## Real-World Timeline

| Milestone | Time | Effort |
|-----------|------|--------|
| Generate basic app | 5 min | Copying/running prompt |
| Working locally | 15 min | First `pnpm dev` |
| Custom features | 30 min | Describing modifications |
| Database scaled | 1 hour | Adding indexes, optimizing |
| Deployed staging | 2 hours | Docker/K8s setup |
| Deployed production | 4 hours | SSL, CDN, monitoring |
| Production hardened | 8 hours | Security, backups, runbooks |
| Ready for 100k users | 1 week | Load testing, optimization |

**Total: 1-2 weeks to production-grade application**

vs. **3-6 months** with traditional development

---

## Next Steps

1. Read `ARCHITECTURE_PROMPT_CONDENSED.md` to understand patterns
2. Choose your domain and feature set
3. Write a prompt following the "Quick Start Template" above
4. Submit to AI assistant with the prompt
5. Review generated code for adherence to architecture
6. Make domain-specific modifications as needed
7. Deploy to staging and validate
8. Iterate and optimize

**Result**: Production-ready, scalable, maintainable application in weeks instead of months.
