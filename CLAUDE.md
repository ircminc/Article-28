# APG 835/837 Rate Analyzer

## What This Project Is
Locally-hostable + cloud-deployable full-stack healthcare billing analytics app for IRC Minc (PMTAC Pvt Ltd). Parses EDI X12 835/837, calculates correct NYS DOH APG reimbursement (Article 28) or CMS MPFS rate (non-Article 28), compares actual paid vs expected, and surfaces compression/variance/denial analytics.

**GitHub repo:** https://github.com/ircminc/Article-28.git  
**Active branch:** `phase-6-deployment` (this is the real trunk — main has unrelated init history)  
**Working directory:** `H:\Working\eProjects\APG Calculator\`

---

## Stack
- **Backend:** Python 3.12 / FastAPI / SQLAlchemy-async / SQLite
- **Frontend:** React 18 + Vite + Tailwind CSS + Recharts
- **Auth:** Argon2 password hashing + JWT tokens, per-user RBAC (admin/analyst/viewer)
- **Export:** openpyxl (Excel), ReportLab (PDF)
- **Container:** Docker Compose (backend + frontend services)
- **CI:** GitHub Actions; Azure Container Apps deploy workflow (wired but dormant)

---

## Project Structure
```
APG Calculator/
├── backend/          # FastAPI app + all business logic
│   ├── data/         # SQLite DB + exported CSVs (init_db.py populates from reference workbook)
│   ├── routers/      # API route handlers
│   ├── engines/      # APG engine, CMS MPFS engine
│   ├── parsers/      # 835I, 835P, 837I, 837P EDI parsers
│   ├── analytics/    # Summary, compression, denials, trends, payer scorecard
│   ├── exporters/    # Excel + PDF exporters
│   └── init_db.py    # DB init — reads reference workbook, seeds all APG tables
├── frontend/         # React 18 + Vite app
│   └── public/       # pmtac-logo.png (company logo)
├── docs/             # Project documentation
├── docker-compose.yml
├── Dockerfile.backend
├── Dockerfile.frontend
└── .env.example
```

---

## Reference Data (APG Engine)
The APG engine is seeded from a NYS DOH Excel workbook:

**Canonical workbook:** `C:\Users\s.patrick\Downloads\Sample APG Fee Calculator - April 2026.xlsx`

Key sheets:
- `HCPCS to EAPGs` — ~18,987 rows
- `ICD-10 DX to EAPGs` — ~73,368 rows
- `Final APG Based Weights` — ~782 rows
- `APG Base Rates` — DTC / freestanding rates
- `Hospital APG Base Rates`
- `Provider County` — ~62 NY counties

Loaded into `backend/data/apg_analyzer.db` and exported as CSV to `backend/data/*.csv` via `init_db.py`. Re-run `init_db.py` when NYS DOH releases an updated workbook.

There is also an updated workbook at:  
`H:\Working\eProjects\Updated APG Fee Calculator - 04232026.xlsx`

---

## Features Shipped (Phases 1–6)
- EDI parsers: 835I, 835P, 837I, 837P with auto-detection
- APG engine: date-scoped EAPG assignment, packaging, multi-proc discounting, U6, capital add-on
- CMS MPFS engine: live API with 24h SQLite cache, rate-limited (**currently broken — see Known Issues**)
- 837↔835 auto-enrichment (diagnosis codes merge + APG re-run)
- Analytics: summary, compression, denials, trends, payer scorecard
- Exporters: multi-sheet Excel + professional PDF
- Rate Calculator: manual CPT/ICD entry with APG + CMS side-by-side, full math transparency, PC/TC component split opt-in
- Auth: per-user login, admin/analyst/viewer RBAC, audit logging
- Settings: provider config, CMS cache refresh, APG workbook re-upload, clear-all-claims danger zone
- Dark/light theme toggle
- Codespaces devcontainer + synthetic data seed + GitHub Actions CI

---

## Known Issues
- **CMS MPFS integration BROKEN:** CMS retired the hardcoded dataset ID (`9767cb68-...`). They migrated PFS data to `pfs.data.cms.gov` with per-year, per-type datasets. Current `cms_engine.py` calls to `data.cms.gov/data-api/v1/dataset/.../data` return 404. Stopgap: graceful banner + default target = APG. Fix needed: rewrite `cms_engine.py` against new `pfs.data.cms.gov` API, update field names, fetch per-year dataset UUIDs, update tests. APG/Article 28 side is completely unaffected.
- **U6 modifier** adjustment factor is a placeholder (0.75) — confirm against NYS DOH PDF before production use.
- **zip_locality table** is empty in Codespaces — CMS lookups need explicit locality or provider config.
- **Frontend bundle** is ~900KB (Recharts heavy) — consider code-splitting if load time matters.

---

## Auth / Secrets
Three GitHub Codespaces secrets configured:
- `APP_JWT_SECRET`
- `APP_ADMIN_USERNAME`
- `APP_ADMIN_PASSWORD`

---

## Git / Push Policy
**Never push to `ircminc/Article-28` without explicit per-push confirmation from the user.** Ask before every push, even if it was approved in a prior session.

Working branch: `phase-6-deployment`. Do not push to `main` directly.

---

## Test Suite
101 backend tests + 1 skip — all passing as of last run.

```bash
cd backend
pytest tests/ -v
```

---

## Deployment
- **Local:** `docker-compose up`
- **Beta:** GitHub Codespaces (current)
- **Production target:** Azure Container Apps (workflow wired, not activated)
