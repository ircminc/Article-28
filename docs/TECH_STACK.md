# Technology Stack — APG 835/837 Rate Analyzer

> NYS DOH Article 28 / APG (Ambulatory Patient Group) compliance + 835/837 EDI
> rate-analysis platform. Built for PMTAC Pvt Ltd's beta-testing cycle on
> Article 28 institutional billing.

This document catalogues the languages, frameworks, libraries, datasets, and
infrastructure choices that make up the application — pinned to the exact
versions in production at the time of writing.

---

## 1 · Backend (Python service)

| Layer | Choice | Version | Why |
|---|---|---|---|
| Language | **Python** | 3.12 | Async/await syntax + improved typing; matches the devcontainer. |
| Web framework | **FastAPI** | 0.115.0 | Async-first; automatic OpenAPI docs; Pydantic-native request/response models. |
| ASGI server | **Uvicorn** (`uvicorn[standard]`) | 0.30.0 | High-performance ASGI; `--reload` in dev, gunicorn-managed in prod images. |
| ORM | **SQLAlchemy** (async) | 2.0.35 | Mapped-style ORM with `Mapped[]` typing; async session via `async_sessionmaker`. |
| DB driver (dev) | **aiosqlite** | 0.20.0 | Async SQLite driver — used for Codespaces dev + lightweight prod. |
| Schema validation | **Pydantic** | 2.9.0 | All request/response/DTO bodies; v2 `ConfigDict` + `Field` validators. |
| Settings | **pydantic-settings** | 2.5.2 | Reads `APP_*` env vars from Codespaces secrets / Docker env. |
| Multipart uploads | **python-multipart** | 0.0.12 | Required by FastAPI's `UploadFile` for the reference-data upload endpoints. |
| Outbound HTTP | **httpx** | 0.27.0 | Async client for the CMS DKAN API + downstream service integrations. |
| `.xlsx` parsing | **openpyxl** | 3.1.5 | All modern Excel files (eMedNY APG Crosswalk, PMTAC APG Fee Calculator). |
| `.xls` parsing | **xlrd** | **1.2.0** (pinned) | Last version supporting the legacy BIFF format NYS DOH uses for `dtc_base_rates_inv.xls` and `history_and_fee_schedule.xls`. |
| PDF generation | **reportlab** | 4.2.4 | Compliance reports + claim-detail PDF exports. |
| Numerics | **pandas** + **numpy** | 2.2.3 / 2.1.2 | Aggregation pipelines for the analytics engine. |
| Date utils | **python-dateutil** | 2.9.0 | Parsing edge-case date formats from EDI segments + Excel cells. |
| Password hashing | **argon2-cffi** | 25.1.0 | Argon2id at OWASP-recommended params for `users.password_hash`. |
| JWT | **PyJWT** | 2.12.1 | Bearer-token sessions; HS256 signed by `APP_JWT_SECRET`. |
| CLI progress | **tqdm** | 4.67.0 | Bulk-loader progress bars on the workbook ingestion CLI. |
| Tests | **pytest** + **pytest-asyncio** | 8.3.3 / 0.24.0 | Async-aware fixtures; `auto` mode (no decorator required). |

### Deciminal precision

All monetary math uses Python's `Decimal` with `ROUND_HALF_UP` at 2 decimal
places for final payment amounts. **Float is never used for money.** This is
a hard project rule — see `_round_money` in `backend/engines/apg_engine.py`.

### Source layout

```
backend/
├── main.py            FastAPI app + every HTTP endpoint
├── auth_routes.py     Login / logout / password / users
├── deps.py            DI: get_session, get_current_user, role guards
├── security.py        argon2 hashing + PyJWT sign/verify
├── db/
│   ├── database.py            SQLAlchemy models + engine + session factory
│   ├── init_db.py             Workbook ingestion CLI (legacy combined file)
│   ├── init_admin.py          Bootstrap initial admin from env vars
│   ├── init_crosswalk.py      eMedNY APG Crosswalk loader (HCPCS + ICD-10)
│   ├── init_weights_history.py   NYS DOH weights + Px + Fee Schedule loader
│   ├── init_dtc_rates.py      NYS DOH DTC base-rates partial loader
│   ├── init_apg_base_rates_v2.py PMTAC Updated APG Fee Calculator loader
│   └── init_zip_locality.py   CMS ZIP → locality mapping
├── engines/
│   ├── apg_engine.py          NYS DOH APG calculation (priority ladder)
│   ├── analytics_engine.py    Variance, compression, peer-group rollups
│   ├── cms_engine.py          CMS MPFS rate fetcher + 24h cache
│   └── claim_linker.py        835 ↔ 837 reconciliation
├── parsers/
│   ├── edi_837.py             Institutional + Professional claim parser
│   ├── edi_835i.py            Institutional remit parser
│   └── edi_835p.py            Professional remit parser
├── exporters/                 Excel / CSV / PDF report generators
└── tests/                     pytest suite (~80 tests at last count)
```

---

## 2 · Frontend (single-page React app)

| Layer | Choice | Version | Why |
|---|---|---|---|
| Framework | **React** | 18.3.1 | Industry standard SPA framework; broad ecosystem. |
| Build tool | **Vite** | 5.4.10 | Instant HMR + fast `dev` startup; Codespaces port 3000. |
| Bundler plugin | **@vitejs/plugin-react** | 4.3.3 | JSX/Fast Refresh integration. |
| Styling | **Tailwind CSS** | 3.4.14 | Utility-first; project palette under `tailwind.config.js`. Dark-mode via `class` strategy. |
| PostCSS chain | **postcss** + **autoprefixer** | 8.4.49 / 10.4.20 | Standard Tailwind-recommended pipeline. |
| Server-state | **@tanstack/react-query** | 5.59.0 | All API calls go through `useQuery` / `useMutation`; cache-keyed invalidations. |
| HTTP client | **axios** | 1.7.7 | Configured `apiClient` instance with JWT bearer interceptor. |
| Routing | **react-router-dom** | 6.27.0 | Client-side routing for the dashboard / upload / settings pages. |
| Headless UI | **@headlessui/react** | 2.1.10 | Accessible dialogs, listboxes, switches — pairs with Tailwind. |
| Icons | **lucide-react** | 0.456.0 | Tree-shakable Feather-derived icons (Upload, CheckCircle2, AlertCircle, etc.). |
| File uploads | **react-dropzone** | 14.2.9 | Drag-and-drop on the EDI / reference-data uploaders. |
| Charts | **recharts** | 2.13.3 | Bar / line / scatter / box-plot for the analytics dashboard. |
| Date utils | **date-fns** | 4.1.0 | Light, tree-shakable date formatting (preferred over moment.js). |
| Linting | **ESLint** | (peer) | `npm run lint` lints `src/**/*.{js,jsx}`. |
| Type definitions | **@types/react**, **@types/react-dom** | 18.3.12 / 18.3.1 | Type hints for IDE intellisense even without TypeScript files. |

JavaScript (not TypeScript) is the source language — though Vite + the
`@types/react` packages give VS Code full type-completion in JSX without
the build-step overhead.

---

## 3 · Persistence

| Aspect | Choice | Notes |
|---|---|---|
| Engine | **SQLite** (default) | Lightweight, zero-admin; ships with the devcontainer. Swappable via `DATABASE_URL`. |
| Async driver | **aiosqlite** | Routes async SQLAlchemy traffic to SQLite without blocking the event loop. |
| Migration strategy | **`Base.metadata.create_all`** at startup | Tables are append-only between releases; column adds rather than schema migrations. Adequate for the current dataset size (~100k rate rows). |
| Production target | Same SQLite or PostgreSQL via `asyncpg` | The async ORM layer is portable; only `DATABASE_URL` changes. |

### Reference-data tables

| Table | Source | Approx. rows |
|---|---|---|
| `apg_base_rates` | PMTAC `Updated APG Fee Calculator` (Updated APG Base Rate sheet) | ~180 |
| `hcpcs_to_eapg` | eMedNY APG Crosswalk (`APGcrosswalkMMDDYYYY.xlsx`, HCPCS to EAPGs sheet) | ~21,000 |
| `icd10_to_eapg` | eMedNY APG Crosswalk (ICD-10 DX to EAPGs sheet) | ~75,000 |
| `apg_weights` | NYS DOH `history_and_fee_schedule.xls` (Final APG Based Weights) | ~21,000 |
| `px_based_weights` | NYS DOH `history_and_fee_schedule.xls` (Final Px Based Weights) | ~5,300 |
| `fee_schedule` | NYS DOH `history_and_fee_schedule.xls` (Fee Schedule) | ~2,100 |
| `provider_county` | NYS DOH county → region map | 62 |
| `cms_rate_cache` | CMS DKAN datastore API (24h TTL) | varies |
| `zip_locality` | CMS ZIP code → MPFS locality mapping | ~40,000 |

---

## 4 · External data sources

| Source | What we pull | Format / API |
|---|---|---|
| **eMedNY** (NY State's Medicaid system) | APG Crosswalk — HCPCS + ICD-10 → EAPG assignments under the Solventum v3.18 taxonomy | `.xlsx` — manual upload, quarterly cadence |
| **NYS DOH** (Department of Health) | Historical APG weights, Px-based weights, flat-fee schedule | `.xls` (legacy BIFF) — manual upload |
| **NYS DOH** | DTC base-rate inventory (`dtc_base_rates_inv.xls`) | `.xls` — manual upload |
| **PMTAC** (compiled internal workbook) | Authoritative DTC base rates (currently effective 2022-04-01) | `.xlsx` — manual upload |
| **CMS Physician Fee Schedule (MPFS)** | Live MPFS rates by HCPCS + locality + year | DKAN datastore API at `pfs.data.cms.gov` — fetched on demand, cached 24 h |

---

## 5 · Infrastructure & DevOps

| Layer | Choice | Notes |
|---|---|---|
| Dev environment | **GitHub Codespaces** | `.devcontainer/devcontainer.json` — Universal image + Python 3.12 feature + Node 20 feature. One-click "Open in Codespace" for new beta testers. |
| Container build | **Docker** | `Dockerfile.backend` (Python 3.12 + uvicorn) + `Dockerfile.frontend` (Node build → static-server multi-stage). |
| Compose | **docker-compose** | `docker-compose.yml` (dev) + `docker-compose.production.yml` (prod). |
| Source control | **Git** + **GitHub** | Repo: `ircminc/Article-28`. Active branch: `phase-6-deployment`. |
| CI / Actions | **GitHub Actions** | `.github/workflows/` — lint + test on push/PR. |
| Secrets | Codespaces / `.env` | `APP_ADMIN_USERNAME`, `APP_ADMIN_PASSWORD`, `APP_JWT_SECRET`, `DATABASE_URL`. Never committed. |

---

## 6 · Domain integrations

| Standard / spec | Where it lives |
|---|---|
| **NYS DOH APG methodology** (Article 28 Outpatient Provider Manual) | `backend/engines/apg_engine.py` — base-rate selection, packaging, multi-procedure discounting, U6 modifier, capital add-on, visit-purpose ICD override |
| **EAPG v3.18** (Solventum-licensed) | `_coerce_eapg_type` in `apg_engine.py` — maps the 25+ v3.18 type names to the engine's canonical 5-state enum |
| **Pricing priority ladder** | Fee Schedule (priority 1) > Px-Based Weight (priority 2) > APG Weight (priority 3) — implemented in `apg_engine.calculate()` |
| **EDI 837 (Health Care Claim)** 5010 | `backend/parsers/edi_837.py` — HL loop walker, HI segment dx extraction, NM1 entity routing |
| **EDI 835 (Remittance Advice)** 5010 | `backend/parsers/edi_835i.py` (institutional) + `edi_835p.py` (professional) |
| **CMS Medicare Physician Fee Schedule** | `backend/engines/cms_engine.py` — DKAN POST-with-conditions queries, locality-aware rate selection |

---

## 7 · Quality / safety practices

- **Decimal-only money math.** No `float` for currency — caught by code review and a custom `_round_money` helper.
- **Argon2id password hashing** at OWASP-recommended cost params; password rotations on `init_admin --force-reset`.
- **Audit log** on every privileged write: reference-data reload, user role change, claim deletion, master reset. Tied to user + IP + timestamp.
- **Role-based access**: `admin` / `analyst` / `viewer` enforced via FastAPI dependency guards (`RequireAdmin`, `RequireAnalyst`, `CurrentUser`).
- **Two-step destructive actions**: Master Reset and Clear Claims both require typed confirmation strings ("RESET" / "DELETE") in the UI before the API call fires.
- **Async sandboxing**: each request opens its own SQLAlchemy `AsyncSession`; no shared mutable state across requests.
- **Source-of-truth data uploads** are admin-only and ID-stamped in `audit_log.action` (`crosswalk.reload`, `weights_history.reload`, `apg_base_rates_v2.reload`, etc.).
- **Tests**: ~80 cases across loaders, parsers, engine math (priority ladder, visit-purpose override, ICD normalization), API endpoints. `pytest-asyncio` in `auto` mode; in-memory SQLite fixtures for fast iteration.

---

## 8 · What we deliberately don't use (and why)

| Avoided | Why |
|---|---|
| **TypeScript** in the frontend | The team doesn't currently have a TS workflow; `@types/react` gives 80% of the IDE benefit without the build overhead. Revisit if/when team comfort grows. |
| **Alembic / formal migrations** | Schema is append-only between releases and the dataset is small enough that `create_all` + manual delete-and-reload is faster to iterate on. Plan to introduce Alembic when we ship to a managed Postgres. |
| **Redux / Zustand / Pinia-style global state** | TanStack Query covers server state; React's `useState` covers ephemeral UI state. No need for a third store. |
| **GraphQL** | The data model is small enough that hand-rolled REST endpoints are easier to reason about and audit-log. |
| **floats for money** | Hard rule. See section 1. |
| **`pip install --user` / system pip** | All deps installed inside the devcontainer / Docker image so we don't pollute the host. |

---

*Last updated: 2026-04-23 — version `phase-6-deployment` branch.*
