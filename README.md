# APG 835/837 Rate Analyzer

Healthcare billing analytics for NYS Medicaid Article 28 reimbursement.
Parses Electronic Remittance Advice (**835I**) and Claim (**837**) files
against the NYS DOH APG methodology to detect underpayments, packaging
errors, and compression.

**Status:** Phase 6 (Codespaces + GitHub Actions). Codespaces-ready for team
testing today, Azure Container Apps wired for the eventual production move.

**Quick paths to try it:**

| Scenario | Follow |
|---|---|
| "Let my team click a URL right now" | [DEPLOY.md §1 — Codespaces](./DEPLOY.md#1-team-testing-on-github-codespaces) |
| "Run it locally on my Windows box" | [§Getting started (native)](#getting-started-native) below |
| "Ship to a HIPAA-eligible Azure URL" | [DEPLOY.md §3 — Azure Container Apps](./DEPLOY.md#3-azure-container-apps-production) |

---

## What it does

For each 835I claim the analyzer:

1. Parses the EDI X12 envelope, claim (`CLP`), service lines (`SVC`), and
   adjustments (`CAS`).
2. Looks up the correct EAPG for each HCPCS/CPT code on the line, date-scoped
   to the date of service via the NYS DOH HCPCS → EAPG crosswalk.
3. Selects the correct APG weight (`Final APG Based Weights` table, honoring
   the `final_rate` / `year_rate` override when applicable).
4. Selects the correct base rate for the provider's peer group, region
   (Upstate / Downstate), and date of service, from the `APG Base Rates`
   (DTC) or `Hospital APG Base Rates` tables.
5. Applies packaging rules (Incidental EAPGs, zero-weight lines, medical
   visits bundled into significant procedures).
6. Applies multi-procedure discounting (primary significant procedure at
   100%, additional significant procedures at 50%).
7. Applies modifier U6 adjustment when present.
8. Adds capital add-on if the provider is eligible.
9. Returns `(correct_apg_payment, actual_paid, variance, compression_pct)`
   plus a per-line explanation of which rules fired.

All monetary math uses `Decimal` with ROUND_HALF_UP; weights and rates are
loaded from the NYS DOH reference workbook and versioned as CSV snapshots.

---

## Regulatory context

- **Article 28** of NY Public Health Law governs licensed outpatient
  facilities (DTCs, freestanding clinics, hospital outpatient departments)
  billed institutionally — those claims generate 835I remittances.
- **APG Payment Formula:**
  `APG Payment = APG Relative Weight × Base Rate × Payment Modifiers + Capital Add-On`
- Rate codes, peer groups, regions, and all weight/rate histories are
  administered by the **NYS DOH**. See references below.

**Reference URLs:**
- APG Provider Manual — https://www.health.ny.gov/health_care/medicaid/rates/apg/index.htm
- Freestanding DTC base rates — https://www.health.ny.gov/health_care/medicaid/rates/apg/rates/dtc/dtc_base_rates_inv.htm
- U6 Modifier Policy — https://www.health.ny.gov/health_care/medicaid/rates/policy/u6_modifier_policy.htm

---

## Repository layout

```
Article-28/
├── backend/                         # FastAPI + SQLite
│   ├── main.py                      # HTTP API entry point
│   ├── requirements.txt
│   ├── pytest.ini
│   ├── db/
│   │   ├── database.py              # SQLAlchemy async ORM
│   │   └── init_db.py               # Loads reference data from the workbook
│   ├── models/schemas.py            # Pydantic v2 DTOs
│   ├── parsers/edi_835i.py          # 835I EDI X12 parser
│   ├── engines/apg_engine.py        # APG calculation engine
│   ├── tests/                       # pytest suite (19 tests, all passing)
│   └── data/                        # Generated CSV snapshots + SQLite DB (gitignored)
├── frontend/                        # React + Vite + Tailwind SPA
│   ├── package.json
│   ├── vite.config.js
│   ├── tailwind.config.js
│   ├── index.html
│   └── src/
│       ├── main.jsx
│       ├── App.jsx                  # Router + layout
│       ├── index.css                # Tailwind + component classes
│       ├── components/              # Layout, Sidebar, HealthBadge, FileDropZone
│       ├── pages/                   # Dashboard, Upload, Claims, ClaimDetail, Settings
│       ├── services/api.js          # axios client + React Query hooks
│       └── utils/format.js          # currency / date / pct formatters
├── Dockerfile.backend
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

## Getting started (native)

### 1. Install Python dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 2. Load reference data from the source workbook

The application does **not** ship with reference data — it must be loaded
from a NYS DOH / APG Excel workbook. The loader expects the following sheets:

- `HCPCS to EAPGs`
- `ICD-10 DX to EAPGs`
- `Final APG Based Weights`
- `APG Base Rates` (Freestanding/DTC)
- `Hospital APG Base Rates`
- `Provider County`

```bash
# From the repo root
python -m backend.db.init_db --workbook "C:/path/to/workbook.xlsx"
```

This:
- (Re)creates the SQLite schema at `backend/data/apg_analyzer.db`
- Loads all 6 reference tables
- Writes CSV snapshots to `backend/data/*.csv` for audit
- Prints row-count totals

**Expected totals** for the April 2026 workbook:
```
hcpcs_to_eapg       18986
icd10_to_eapg       73367
apg_weights         18200    (long-form: APG × effective_date)
apg_base_rates       336     (long-form: source × peer × region × effective_date)
provider_county       62
```

### 3. Start the API

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000/docs` for the auto-generated OpenAPI UI.

### 4. Configure a provider

```bash
curl -X POST http://localhost:8000/api/config/provider \
  -H 'Content-Type: application/json' \
  -d '{
    "provider_name": "Sample Clinic LLC",
    "npi": "1234567890",
    "county_code": 61,
    "peer_group": "Clinic*",
    "provider_type": "dtc",
    "capital_addon_eligible": false
  }'
```

### 5. Upload an 835I file

```bash
curl -X POST http://localhost:8000/api/upload/835i \
  -F "files=@backend/tests/fixtures/sample_835i.edi"
```

### 6. Inspect results

```bash
curl http://localhost:8000/api/claims
curl http://localhost:8000/api/claims/1/apg
```

### 7. Start the frontend (Phase 3)

```bash
cd frontend
npm install
npm run dev
```

Then browse to <http://localhost:3000>. Vite proxies `/api/*` to the FastAPI
backend on :8000 by default; override with `VITE_API_URL=...` if running the
backend elsewhere.

The SPA covers:

- **Dashboard** — totals, claim-type mix, recent uploads (full analytics
  charts arrive with the Phase 4 analytics endpoints)
- **Upload** — three drop zones (835I / 835P / 837). Uploads trigger the
  same 837↔835 auto-enrichment as the API.
- **Claims** — searchable, file-type-filterable, paginated table with
  linked-sibling indicators
- **Claim detail** — header totals, APG calculation breakdown with per-line
  packaging/discount/U6 flags, service lines, adjustments, linked sibling
- **Settings** — provider configuration (name, NPI, county → region,
  peer group, provider type, capital add-on, CMS locality)

A `HealthBadge` in the header polls `/api/health` every 30s so you can tell
at a glance whether the backend is up and the reference data is loaded.

---

## Getting started (Docker)

```bash
# Build both images
docker compose build

# Start backend + frontend
docker compose up -d

# One-time reference-data load — point WORKBOOK_DIR at the directory holding the .xlsx
WORKBOOK_DIR=/path/to/dir docker compose run --rm backend \
    python -m backend.db.init_db --workbook /app/workbooks/your_workbook.xlsx

# Verify
curl http://localhost:8000/api/health     # backend
open http://localhost:3000                # frontend (Windows: start)
```

---

## API surface

| Method | Path                                 | Phase | Description                                     |
|--------|--------------------------------------|-------|-------------------------------------------------|
| GET    | `/api/health`                        | 1     | Liveness + reference-data status                |
| POST   | `/api/config/provider`               | 1     | Upsert active provider config                   |
| GET    | `/api/config/provider`               | 1     | Get active provider config                      |
| POST   | `/api/upload/835i`                   | 1     | Upload & analyze 835I institutional remittance  |
| POST   | `/api/upload/835p`                   | 2     | Upload 835P professional remittance             |
| POST   | `/api/upload/837`                    | 2     | Upload 837I/P claim submissions (auto-detected) |
| GET    | `/api/claims`                        | 1     | List parsed claims (paginated)                  |
| GET    | `/api/claims/{id}`                   | 1/2   | Claim detail + APG result + linked sibling      |
| GET    | `/api/claims/{id}/apg`               | 1     | APG result for a single claim                   |
| GET    | `/api/reference/hcpcs/{code}?dos=`   | 1     | HCPCS → EAPG lookup                             |
| GET    | `/api/reference/icd10/{code}?dos=`   | 1     | ICD-10 → EAPG lookup                            |
| GET    | `/api/reference/apg/{apg}?dos=`      | 1     | APG weight lookup                               |
| GET    | `/api/reference/base-rates`          | 1     | List base rates (filterable)                    |
| GET    | `/api/reference/cms/{code}?dos=`     | 2     | CMS MPFS rate (cached, live fallback)           |
| GET    | `/api/reference/zip-locality/{zip}`  | 2     | ZIP → Medicare locality                         |
| GET    | `/api/analytics/summary`             | 4     | KPI totals (billed / paid / denial / variance)  |
| GET    | `/api/analytics/compression`         | 4     | Rate compression by EAPG / procedure / peer group |
| GET    | `/api/analytics/denials`             | 4     | CARC analysis ranked by dollar impact           |
| GET    | `/api/analytics/trends`              | 4     | Monthly / quarterly billed / paid / variance    |
| GET    | `/api/analytics/payer-scorecard`     | 4     | Per-payer KPIs                                  |
| POST   | `/api/export/excel`                  | 4     | Multi-sheet .xlsx report download               |
| POST   | `/api/export/pdf`                    | 4     | Professional PDF report download                |

### Upload behavior & auto-enrichment (Phase 2)

Every 835I / 835P / 837 upload triggers the claim linker:
- When a claim ID appears in both an 837 submission and an 835 remittance,
  the two records are linked via `linked_claim_id_fk`.
- The 835 is enriched with diagnosis codes from the matching 837 (if it
  didn't already have them).
- The APG engine re-runs on the enriched 835 record automatically.

This makes it safe to upload in either order — 835 first then 837, or 837
first then 835. The final APG result reflects the full context.

---

## Testing

```bash
cd backend
pytest -v
```

Test categories:
- **Lexer / parser** — delimiter detection, segment extraction, CLP/SVC/CAS decoding
- **Reference lookups** — date-scoped HCPCS, ICD-10, APG weight, base rate
- **APG engine** — structural integrity, packaging, discounting, region resolution

Engine tests skip automatically if `python -m backend.db.init_db` hasn't been
run yet (they require a populated reference DB).

---

## Design notes

### Reference data: long form vs. wide form

The spec calls for wide tables like `weight_jan2010, weight_apr2010, ...`.
We load long form instead: one row per `(apg, effective_date)`. This keeps
the date-scoped selector a one-liner (`WHERE effective_date <= :dos ORDER BY
effective_date DESC LIMIT 1`) and makes NYS DOH's periodic weight updates an
`INSERT` instead of a schema migration.

Same for base rates — one row per `(source, peer_group, region, effective_date)`.

### Decimal throughout

Every monetary value flows as `Decimal`, quantized to cents with
ROUND_HALF_UP on exit. Float never touches the money path.

### PHI handling

- Uploaded EDI files and the SQLite DB live in `backend/data/`,
  `backend/uploads/` — all `.gitignore`d.
- The application makes no outbound network calls except (in Phase 2)
  the public CMS MPFS API, which is passed procedure codes only — no PHI.
- Application logs use SQLAlchemy's defaults; no claim IDs or patient
  names are logged by our own code.

### Known gaps vs. the original spec

- **Modifier U6 adjustment factor** is a placeholder (0.75); confirm against
  the NYS DOH policy PDF in Phase 2.
- **835P, 837I, 837P** parsers are deferred to Phase 2.
- **CMS MPFS engine** is deferred to Phase 2.
- **React frontend** is deferred to Phase 3.
- **Excel / PDF exports** and the **analytics engine** are deferred to Phase 4.

---

## Roadmap

| Phase | Scope                                                          | Status   |
|-------|----------------------------------------------------------------|----------|
| 1     | Backend foundation, 835I parser, APG engine, reference data    | ✅ landed |
| 2     | 835P + 837 parsers, CMS MPFS engine, 837↔835 auto-enrichment   | ✅ landed |
| 3     | React + Vite + Tailwind SPA (Dashboard, Upload, Claims)        | ✅ landed |
| 4     | Analytics engine, Excel + PDF exporters, Reports page, Recharts | ✅ landed |
| 5     | Per-user login, RBAC, audit log, hardened Docker, cloud deploy guide | ✅ landed |
| 6     | Codespaces devcontainer, synthetic-data seed, GitHub Actions CI + dormant Azure deploy | ✅ landed |

### Loading CMS data (Phase 2 — optional)

The CMS MPFS rate lookup needs a ZIP → locality mapping. Download the annual
"ZIP Code to Carrier Locality File" from
<https://www.cms.gov/medicare/medicare-fee-service-payments/physicianfeesched/zip-code-carrier-locality-file>
and load it:

```bash
python -m backend.db.init_zip_locality --file "C:/path/to/ZIP5_OCT2025.xlsx"
```

The loader is tolerant to column-name variations across annual releases. CSV
files are also accepted. If you don't load this file, CMS lookups still work
as long as you pass `?locality=...` explicitly or set `cms_locality` in the
provider config.

---

## License

TBD — copyright IRC Minc. Contact the maintainer before distributing.

---

## Contributing

Pull requests should include:
- Passing test suite (`pytest backend/tests/`)
- No new dependencies without discussion
- Decimal-safe money math — never `float` in the payment path
- PHI never committed — `.gitignore` is strict for a reason
