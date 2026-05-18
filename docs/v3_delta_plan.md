# APG 835/837 Billing Intelligence Platform — v3 Delta Plan

**Prepared:** 2026-04-23
**Audience:** IRC Minc engineering + product
**Status:** Proposal — no code changes until this plan is green-lit
**Target repository:** `https://github.com/ircminc/Article-28` (branch: `phase-6-deployment`)
**Target deployment:** Azure (beta → production)

---

## 1. Purpose

This document reconciles the v3 specification
(`Claude_Code_Prompt_Complete_v3.md`, 2157 lines) against the current
code base (Phases 1–7 shipped), adds five Tier-1 analytics modules the
v3 spec omits, scopes an optional CMS-1500 / UB-04 form reader, and
bakes Azure compatibility into every workstream.

The goal is a **single consolidated scope** the team can cost, sequence,
and sign off against before any implementation begins.

---

## 2. What is already shipped (Phases 1–7)

| Area | State | Evidence |
|---|---|---|
| EDI parsers (835I, 835P, 837I+P combined) | ✅ Complete | `backend/parsers/edi_835i.py`, `edi_835p.py`, `edi_837.py` |
| APG engine | ✅ Exceeds v3 spec | Fee Schedule > Px Weight > APG Weight priority ladder; v3.18 EAPG type mapping; date-effective tables |
| CMS MPFS engine | ✅ Complete | DKAN-based lookups, 24h cache, exponential backoff |
| Reference-data loaders | ✅ Native NYS DOH formats | `init_crosswalk.py`, `init_weights_history.py`, `init_dtc_rates.py`, `init_db.py` (legacy) |
| Auth (JWT, Argon2id, role model) | ✅ Complete | Not in v3 spec at all — a net addition |
| Analytics engine (4 of 15 modules) | ⚠️ Partial | Denials (A), Payer Scorecard (C), APG Compression (D), FPRR partial (M) |
| Exporters (Excel + PDF, MVP) | ⚠️ Partial | 6-sheet Excel, 5-page PDF — v3 wants 5 separate exporters (7+5+25 sheets, 23-page PDF, ZIP) |
| Frontend (8 pages, no Analytics) | ⚠️ Partial | Dashboard, Upload, Claims, ClaimDetail, Reports, Settings, Login, Users built; no `Analytics.jsx` |
| Form renderers (UB-04, CMS-1500) | ❌ Missing | No `backend/renderers/` folder |
| Form reader (OCR / vision ingestion) | ❌ Missing | Not in v3 — proposed as Part 6 below |
| CI/CD scaffolding for Azure | ✅ Partial | `.github/workflows/deploy-azure.yml`, `docker-compose.production.yml` references Key Vault |

**Key deviation from v3 worth preserving:** the native NYS DOH / eMedNY
file loaders. v3 assumes CSVs seeded from `backend/data/`. We have
something better — operators upload the state's published files
directly. The plan below protects this.

---

## 3. Gap analysis and effort estimates

All hours are for a single developer. Two-developer parallelism
reduces wall-clock time but not total hours meaningfully (the work is
mostly sequential through a shared `AnalyticsFilterContext`).

### 3.1 Analytics engines — Modules A through O (v3 Part 8)

**Hard requirement per product owner.** These are the billing-audit
value proposition.

| Module | Current | Gap | Hours |
|---|---|---|---|
| A Denials | ✅ Basic | Add trend, by-provider, first-pass denial rate | 3 |
| B ERA Remarks (RARC) | ❌ | Full build — top codes, RARC×CARC co-occurrence matrix, actionable patterns | 5 |
| C Payer Scorecard | ✅ Basic | Add appeal success rate, write-off rate, avg days to payment | 3 |
| D APG Compression | ✅ Basic | Add by-peer-group, by-payer, standalone-denial audit | 3 |
| E CPT vs CMS | ❌ | Full build — uses existing CMS engine; charge adequacy zones | 6 |
| F Claim Integrity / Clean Claim Rate | ❌ | Full build — clean claim %, NCCI edits, POS issues, rev-code/HCPCS mismatch | 6 |
| G Revenue Leakage | ❌ | Full build — waterfall, recoverable-vs-non, per-category totals | 5 |
| H Timely Filing | ❌ | Full build — CO-29 histogram, per-payer deadlines, recovery window | 4 |
| I Authorization | ❌ | Full build — CO-39/197/252, N4 splits, retro-auth success | 4 |
| J COB | ❌ | Full build — OA-23, CO-22, MA04 detection, cross-over identification | 4 |
| K Modifiers | ❌ | Full build — top modifiers, impact, U6, CO-4 problem pairs | 4 |
| L Write-offs | ❌ | Full build — CO/PR/OA/PI/CR breakdown, recoupment, credit balance | 4 |
| M FPRR | ✅ Partial (trends) | Add by-payer, by-CPT-category, rework cost | 3 |
| N Zero-pay | ❌ | Full build — aging buckets, per-payer, low-pay detection | 4 |
| O Rebilling opportunities | ❌ | Full build — 12 rule-based flags, priority ranking, recovery estimate | 6 |
| **Subtotal** | | | **64** |

### 3.2 Tier-1 extended analytics — Modules P through T (**new, not in v3**)

These five are table stakes for an RCM audit tool. They are pure SQL on
data already parsed and add credibility with anyone who has used
Waystar, Experian Health, or similar.

| Module | Purpose | Hours |
|---|---|---|
| P Collection rates | Net + Gross collection rate (industry standard RCM KPIs) | 3 |
| Q Days in A/R + aging | Outstanding claims by age bucket; benchmark <40 days | 4 |
| R E&M Bell Curve | 99202–99215 distribution vs CMS peer benchmarks; RAC audit risk signal | 7 |
| S POS Compliance | Place-of-service vs procedure mismatches; pre-submission edits | 4 |
| T Patient Responsibility | PR assigned vs collected; balance aging; bad-debt candidates | 5 |
| **Subtotal** | | **23** |

### 3.3 API endpoints

~37 new endpoints, almost all thin wrappers over engine methods.
Pattern already established for the four existing analytics endpoints.

- 11 missing analytics modules × ~2.5 endpoints each = ~28 endpoints
- 5 extended-analytics endpoints (P–T) = 5 endpoints
- Export endpoints (ERA detail, Claim detail, Combined, Forms/ZIP, per-module) = 5 endpoints
- Form rendering endpoints (ub04-pdf, cms1500-pdf, form-preview, batch-export) = 4 endpoints
- Reference + config additions (carc/rarc lookup, payer timely-filing config) = 3 endpoints
- **Subtotal: 15 hours**

### 3.4 Exporters

| Exporter | v3 spec | Status | Hours |
|---|---|---|---|
| ERA extraction workbook | 7 sheets (ERA header, claims, service lines, CAS, RARC, PLB, APG results) | ❌ Missing | 6 |
| Claim (837) extraction workbook | 5 sheets (claim header, diagnoses, SV2, SV1, providers, COB, auth/ref) | ❌ Missing | 6 |
| Analytics workbook (25 sheets) | One sheet per module A–O + extended P–T + references | ⚠️ Current 6 sheets | 12 |
| Analytics PDF (23 pages) | One section per module with matplotlib charts | ⚠️ Current 5 pages | 12 |
| Form ZIP bundler | ZIP of multiple UB-04 / CMS-1500 PDFs | ❌ Missing | 2 |
| **Subtotal** | | | **38** |

### 3.5 Form renderers (UB-04 + CMS-1500)

Pixel-accurate PDF rendering. High-precision, low algorithmic
complexity. One-time cost.

| Renderer | Scope | Hours |
|---|---|---|
| UB-04 (CMS-1450) | 81 form locators, multi-page when >22 service lines | 20 |
| CMS-1500 (02/12) | 33 boxes, multi-page when >6 service lines | 18 |
| Shared coordinate/grid helpers | Reused across both | 4 |
| **Subtotal** | | **42** |

### 3.6 Frontend — Analytics.jsx + 20 module tabs

The single biggest line item. Mitigated by shared infrastructure.

| Component | Scope | Hours |
|---|---|---|
| `AnalyticsFilterContext` + sticky FilterBar | Date range, claim type, payer, CPT, CARC, RARC, provider NPI, period | 4 |
| Reusable chart wrappers | Recharts (already installed) — BarChart, LineChart, StackedBar, Donut, Scatter, Histogram, HeatMap, Waterfall, Treemap | 6 |
| 20 module tabs (A–O + P–T) | Each tab: 2–5 charts + 1–2 tables + per-chart PNG download + per-table Excel export | 45 |
| ClaimFormViewer modal | PDF preview (iframe) + download + per-row batch export integration | 5 |
| Dashboard enhancements | 6 KPI cards + 2×2 chart grid using new engine endpoints | 4 |
| Per-chart PNG download + per-table Excel integration | Reused across all tabs | 3 |
| **Subtotal** | | **67** |

### 3.7 Azure migration and deployment (**new, not in v3**)

v3 describes Docker Compose deployment. Azure adds a set of
platform-specific adaptations that must land before production cutover.

| Item | Scope | Hours |
|---|---|---|
| SQLite → PostgreSQL migration | Switch `sqlite+aiosqlite` to `postgresql+asyncpg`; handle type coercions; Azure Database for PostgreSQL Flexible Server provisioning | 10 |
| Alembic migrations | Baseline migration + per-table migration history; required once off SQLite | 6 |
| Blob Storage for uploads/exports | Replace local `uploads/` and `exports/` with `azure-storage-blob` abstraction; signed-URL download flow | 8 |
| Key Vault for secrets | `APP_JWT_SECRET`, DB connection string, CMS API keys via Container Apps secret references | 4 |
| Application Insights wiring | `opencensus-ext-azure` or `azure-monitor-opentelemetry`; structured logs, HIPAA-safe (no PHI in log bodies) | 4 |
| Azure Container Apps Bicep/IaC | Declarative infra: Container Apps env, ingress, scale rules, Key Vault, Postgres, Storage, Log Analytics | 12 |
| GitHub Actions → Azure pipeline | Build image → push to Azure Container Registry → deploy to Container Apps (existing workflow extended) | 6 |
| HIPAA / BAA artifacts | Microsoft Customer Agreement with BAA; documented list of HIPAA-eligible services used; data-flow diagram | Legal — not code |
| Network posture | Private endpoints for Postgres + Storage + Key Vault; Container Apps VNet integration; egress allowlist for `pfs.data.cms.gov` | 6 |
| **Subtotal** | | **56** |

### 3.8 Form reader (CMS-1500 / UB-04 ingestion — new, not in v3)

Purely additive. Outputs the same `ParsedClaim` model as EDI parsers,
so every downstream pipeline (analytics, exporters, renderers) works
unchanged.

| Phase | Scope | Hours |
|---|---|---|
| Phase 1: digital PDF text extraction | `pdfplumber` + known UB-04/CMS-1500 field positions; handles billing-software-generated PDFs (~60–70% of real-world cases) | 22 |
| Phase 2: Azure OpenAI GPT-4 Vision fallback | For scanned/imaged PDFs and handwritten fields; Azure OpenAI is already BAA-covered under Microsoft's cloud agreement | 32 |
| Phase 3: manual-review UI | Side-by-side original vs extracted; reviewer corrects before commit; required for any handwritten input | 20 |
| Confidence scoring + routing | Route low-confidence extractions to review queue automatically | 6 |
| **Subtotal (all 3 phases)** | | **80** |

**Azure-specific benefit:** Azure OpenAI Service provides GPT-4 Vision
inside the Microsoft BAA boundary. No separate AI-vendor BAA needed. No
cross-cloud egress. The Phase-2 vision path is materially simpler on
Azure than it would be on AWS (Bedrock) or on-prem.

**Decision gate:** Phase 1 can ship independently. Phases 2 and 3 can
be deferred to a post-v3 enhancement if beta customers don't need
scanned-form support on day one.

---

## 4. Totals and sequencing

### 4.1 Effort roll-up

| Workstream | Hours |
|---|---|
| Analytics engines A–O | 64 |
| Extended analytics P–T | 23 |
| API endpoints | 15 |
| Exporters | 38 |
| Form renderers (UB-04, CMS-1500) | 42 |
| Frontend Analytics + tabs | 67 |
| Azure migration + deployment | 56 |
| **Core v3+ delta** | **305** |
| Form reader Phase 1 (optional, digital PDFs) | 22 |
| Form reader Phases 2+3 (optional, vision + review) | 58 |
| **Total including full form reader** | **385** |

### 4.2 Recommended six-sprint sequence

Each sprint is one calendar week at ~40 developer-hours, producing a
visible, demoable deliverable.

| Sprint | Theme | Scope | Hours |
|---|---|---|---|
| **1** | Shared infrastructure | `AnalyticsFilterContext`, Recharts wrapper library, `Analytics.jsx` shell with tab routing, finish Modules A/C/D to their v3 specs, SQLite→PostgreSQL migration + Alembic baseline | 40 |
| **2** | High-value analytics block 1 | Modules B, E, F, G (engines + endpoints + tabs); Azure Blob Storage for uploads/exports | 40 |
| **3** | High-value analytics block 2 | Modules H, I, J, K (engines + endpoints + tabs); Key Vault + App Insights wiring | 40 |
| **4** | High-value analytics block 3 + extended | Modules L, N, O (engines + endpoints + tabs); extended Modules P–T; Azure Container Apps IaC | 40 |
| **5** | Forms + exporters | UB-04 + CMS-1500 renderers, ERA/Claim raw-extraction workbooks, form ZIP bundler, 25-sheet analytics workbook, 23-page analytics PDF, network posture (private endpoints) | 45 |
| **6** | Integration + beta launch | End-to-end testing with real payer files, HIPAA artifacts, beta documentation, bug fix buffer, (optional) form-reader Phase 1 | 40 |
| **Total** | | | **~245 planned + 60 buffer/stretch** |

Form-reader Phases 2+3 are explicitly **outside this six-sprint plan**
and would follow as a Phase 8 enhancement cycle.

---

## 5. Azure compatibility — baked-in requirements for every workstream

These are not a separate workstream — they are constraints every
sprint must honor. Listing them once here so they are not forgotten in
individual module work.

### 5.1 Service selection (all HIPAA-eligible under Microsoft BAA)

| Layer | Azure service | Rationale |
|---|---|---|
| Compute | **Azure Container Apps** | Closest analog to Docker Compose; Dapr-optional; scales to zero; cheaper than AKS for this workload size |
| Database | **Azure Database for PostgreSQL Flexible Server** | Managed, HIPAA-eligible, async-friendly via `asyncpg`, point-in-time restore |
| Object storage | **Azure Blob Storage (hot tier)** | Uploads, exports, rendered form PDFs; signed URLs for download |
| Secrets | **Azure Key Vault** | JWT secret, DB URL, API keys; Container Apps secret references |
| Observability | **Azure Monitor + Application Insights + Log Analytics** | Centralized logs; custom metrics; alerting |
| AI (form reader Phase 2) | **Azure OpenAI Service (GPT-4 Vision)** | BAA-covered; same subscription; no cross-cloud egress |
| CI/CD | **GitHub Actions → Azure Container Registry → Container Apps** | Already scaffolded in `.github/workflows/deploy-azure.yml` |
| Identity (future) | **Microsoft Entra ID** | Optional post-beta; current JWT stack is fine for launch |

### 5.2 Code constraints that apply to every sprint

1. **No local filesystem writes** outside of `/tmp`. All persistent
   artifacts (uploads, exports, rendered PDFs) go to Blob Storage via
   a thin `StorageBackend` abstraction with a local-disk implementation
   for dev and a Blob implementation for Azure. Switch per env var.

2. **No SQLite-specific SQL**. No `AUTOINCREMENT` shorthand, no
   `INSERT OR IGNORE`, no boolean-as-integer assumptions. All DB
   interaction stays within SQLAlchemy Core/ORM so the dialect is
   transparent. Tests run on both SQLite (CI) and Postgres (staging)
   from Sprint 1 onward.

3. **No PHI in logs.** Never log patient names, DOBs, member IDs, or
   claim IDs in application logs. Use anonymized batch IDs and hashed
   identifiers. Application Insights sampling must not capture request
   bodies containing ERA/claim data.

4. **Decimal for money** throughout (already enforced). Postgres
   `NUMERIC(12,2)` mirrors current SQLAlchemy `Numeric(12, 2)` — no
   change needed at the model layer.

5. **Timezone-aware datetimes** only. Postgres `TIMESTAMPTZ` requires
   it; SQLite tolerated naïve datetimes silently. Audit all
   `datetime.utcnow()` calls and replace with `datetime.now(timezone.utc)`
   during Sprint 1's Postgres migration.

6. **Config from environment** only, never from code. Every Azure-only
   integration (Blob, Key Vault, App Insights, OpenAI) checks for its
   connection string at startup and falls back to a dev-friendly local
   implementation when absent, so local development in a Codespace or
   on a laptop still works without an Azure subscription.

7. **Migrations via Alembic**, introduced in Sprint 1. Every subsequent
   schema change ships as a migration file reviewed in the same PR as
   the model change. No more `Base.metadata.create_all` for production.

8. **Egress allowlist** documented per-service. Backend needs outbound
   HTTPS to `pfs.data.cms.gov` (CMS DKAN), Azure Blob, Azure OpenAI,
   Key Vault, and Monitor endpoints. Everything else is blocked at the
   Container Apps environment level.

### 5.3 HIPAA / BAA posture

- **Microsoft Customer Agreement** with a signed BAA must be in place
  before any PHI touches the Azure environment. This is a
  procurement/legal step, not engineering — flag it to leadership at
  Sprint 1 kickoff to avoid blocking production cutover at the end.
- **HIPAA-eligible services only.** Every service in §5.1 is on
  Microsoft's published HIPAA-eligible list as of 2025-Q4. Verify each
  at the time of provisioning.
- **Data residency** — pin all services to a single US region
  (recommend East US 2 or West US 2 for NYC-based customers).
- **Encryption** — at rest (Azure-managed keys sufficient for beta;
  customer-managed keys optional post-launch), in transit (TLS 1.2+
  enforced by Container Apps ingress).
- **Audit log** — the application's existing audit table migrates to
  Postgres. Azure activity logs cover infrastructure changes. Retention
  ≥6 years to satisfy HIPAA minimums.

---

## 6. Risks and decisions required before kickoff

### 6.1 Technical risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| PostgreSQL migration surfaces hidden SQLite-isms | Medium | Medium | Run full test suite against Postgres in CI from Sprint 1; fix drift incrementally rather than in one big bang |
| Form renderer coordinates drift from official NUBC/NUCC layout | Medium | Medium | Validate rendered output against official form images at the end of Sprint 5; budget is conservative and absorbs 1–2 iterations |
| 15 analytics module tabs feel cluttered | Medium | Low | Design review at end of Sprint 1 using the shell page with mock data; consolidate tabs if five or more share obvious themes |
| Recharts can't render v3-specified chart types (e.g., waterfall) elegantly | Low | Low | Waterfall implementable as stacked bar with transparent segments; heatmap via custom SVG; fallback to matplotlib-rendered PNG for a few exotic charts if needed |
| Azure OpenAI region availability delays Phase 2 form reader | Low | Low | Phases 2–3 are explicitly out of the core six-sprint plan |
| CMS DKAN endpoint changes mid-sprint | Low | High | Already hardened with 24h cache + fallback UUIDs; monitor in App Insights after cutover |

### 6.2 Decisions needed from product owner before Sprint 1

1. **Recharts vs Chart.js.** Recharts is already installed and functional. Switching costs ~12h with no user-visible benefit. **Recommendation: keep Recharts, document the deviation from v3 spec.** Confirm or override.
2. **Form reader scope.** Include Phase 1 in the six-sprint plan, or defer the entire form reader to a Phase 8 enhancement cycle? **Recommendation: defer — ship v3 analytics first, sell the form reader as a follow-on differentiator.**
3. **Azure subscription and BAA.** Who owns the Microsoft Customer Agreement + BAA signing? When does legal expect it done? **Needed no later than end of Sprint 4 to avoid blocking production cutover.**
4. **Azure region.** East US 2, Central US, West US 2? Driven by customer geography (all NY-based for now → East US 2 minimizes latency).
5. **Database approach during beta.** Option A: Postgres from Sprint 1 in both dev and prod. Option B: keep SQLite in dev/Codespace, use Postgres only in Azure. **Recommendation: Option A** — avoids two-dialect drift and forces test coverage to catch Postgres issues early.
6. **Tiered analytics budget.** All 20 modules (A–O + P–T) in the six-sprint plan, or drop some to hit a tighter date? **Recommendation: all 20 — the extended five are each <7h and represent the tool's differentiators versus existing RCM vendors.**

---

## 7. What this plan preserves from existing work

These are explicit non-reversals. The v3 spec would, taken literally,
roll some of this back. This plan does not.

- Native NYS DOH / eMedNY uploaders (`init_crosswalk.py`,
  `init_weights_history.py`, `init_dtc_rates.py`) — v3 assumes static
  CSVs in `backend/data/`. We keep the native loaders; CSV fallback
  becomes deprecated.
- Auth system (JWT, Argon2id, role model, audit log) — v3 doesn't mention
  auth at all. We keep what exists.
- Fee Schedule > Px Weight > APG Weight priority ladder in the APG
  engine — v3 describes only the classic APG formula. Ours is richer
  and more accurate against real NYS DOH data. Kept.
- v3.18 EAPG type coercion — v3 lists only the legacy five types. We
  added a mapping for v3.18's 25+ subtypes. Kept.
- Existing deployment scaffolding (`docker-compose.production.yml`,
  `deploy-azure.yml`, Codespace devcontainer) — all extended, not
  replaced.

---

## 8. What happens next

1. **Review and feedback** on this document. Target: ≤1 week.
2. **Decisions 1–6 (§6.2)** answered by product owner.
3. **Sprint 1 kickoff** with a dated start. First deliverable is the
   Postgres migration + shared Analytics infrastructure + the Analytics
   shell page with Modules A/C/D rebuilt to v3 spec. Visible progress
   in week 1.
4. **Weekly demo** at end of each sprint. If any sprint falls more than
   10h behind, flag and rescope rather than accumulate debt.
5. **Cutover to Azure production** at end of Sprint 6, gated on BAA
   signed and HIPAA artifacts complete.

---

## Appendix A — Module-to-sprint map

| Sprint | Analytics modules shipped | Infra milestones |
|---|---|---|
| 1 | A, C, D upgraded to v3 spec | Postgres, Alembic, AnalyticsFilterContext, Recharts wrappers, Analytics.jsx shell |
| 2 | B, E, F, G | Blob Storage abstraction |
| 3 | H, I, J, K | Key Vault, Application Insights |
| 4 | L, N, O, P, Q, R, S, T | Container Apps Bicep, GitHub Actions → ACR → Container Apps |
| 5 | — (forms + exporters week) | Private endpoints, network posture |
| 6 | — (integration + launch) | BAA sign-off, HIPAA artifacts, beta docs |

## Appendix B — Out of scope for v3 (deferred to Phase 8+)

- Form reader Phases 2 (Azure OpenAI vision) and 3 (manual review UI)
- Tier-3 advanced analytics: denial prediction (ML), upcoding risk
  scorecard, anomaly detection on payer behavior, payment velocity
  forecasting
- Microsoft Entra ID / SSO integration (current JWT stack suffices
  for beta and production v1)
- Multi-tenancy at the DB level (current model is single-tenant)
- Real-time 277CA claim-status ingestion
- 270/271 eligibility verification integration

---

*End of plan.*
