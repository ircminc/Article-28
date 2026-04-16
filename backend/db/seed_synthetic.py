"""Seed the reference tables with *synthetic* data.

Purpose: let the app function in environments where the real NYS DOH workbook
isn't available — Codespaces demos, CI smoke tests, team onboarding. The
numbers here are **fabricated**, not NYS DOH-published values. The APG engine
still computes everything correctly; the inputs just don't reflect real policy.

Usage:
    python -m backend.db.seed_synthetic

The seed covers:
    - ~25 common HCPCS codes (E/M, procedures, labs) mapped to plausible EAPGs
    - ~10 common ICD-10 diagnoses mapped to EAPGs
    - ~15 EAPG weights spanning 'Significant Procedure', 'Medical Visit',
      'Ancillary', 'Incidental' types
    - Base rates for Clinic* / Amb Surg × Upstate / Downstate (DTC source)
    - All 62 NY counties with region flags

Run after `init_db.py` creates the schema. This loader is DESTRUCTIVE — it
wipes the reference tables first so subsequent re-runs are idempotent.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import delete

from backend.db.database import (
    ApgBaseRate,
    ApgWeight,
    Base,
    HcpcsToEapg,
    Icd10ToEapg,
    ProviderCounty,
    async_session,
    engine,
)

log = logging.getLogger("seed_synthetic")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")


# ---------------------------------------------------------------------------
# Synthetic data — fabricated, not NYS DOH values
# ---------------------------------------------------------------------------

# (hcpcs, description, eapg, eapg_desc, eapg_type, eapg_category)
HCPCS = [
    ("99213", "Office visit, established, low complexity", 491, "MEDICAL VISIT INDICATOR", "Incidental", "Incidental procedures and services"),
    ("99214", "Office visit, established, moderate complexity", 491, "MEDICAL VISIT INDICATOR", "Incidental", "Incidental procedures and services"),
    ("99215", "Office visit, established, high complexity", 491, "MEDICAL VISIT INDICATOR", "Incidental", "Incidental procedures and services"),
    ("99203", "Office visit, new, low complexity", 491, "MEDICAL VISIT INDICATOR", "Medical Visit", "Medical visits"),
    ("99204", "Office visit, new, moderate complexity", 491, "MEDICAL VISIT INDICATOR", "Medical Visit", "Medical visits"),
    ("99284", "ED visit, moderate severity", 451, "EMERGENCY DEPARTMENT VISIT", "Significant Procedure", "Emergency department"),
    ("99285", "ED visit, high severity", 452, "EMERGENCY DEPARTMENT VISIT - HIGH", "Significant Procedure", "Emergency department"),
    ("17000", "Destruction of benign lesion, first lesion", 321, "LEVEL I SKIN PROCEDURE", "Significant Procedure", "Dermatologic procedures"),
    ("17003", "Destruction of benign lesions, 2-14", 321, "LEVEL I SKIN PROCEDURE", "Add-On", "Dermatologic procedures"),
    ("11042", "Debridement, skin and subcutaneous tissue", 322, "LEVEL II SKIN PROCEDURE", "Significant Procedure", "Dermatologic procedures"),
    ("11043", "Debridement, muscle and/or fascia", 323, "LEVEL III SKIN PROCEDURE", "Significant Procedure", "Dermatologic procedures"),
    ("45378", "Colonoscopy, diagnostic", 271, "LEVEL I ENDOSCOPY", "Significant Procedure", "Gastrointestinal endoscopy"),
    ("45380", "Colonoscopy with biopsy", 272, "LEVEL II ENDOSCOPY", "Significant Procedure", "Gastrointestinal endoscopy"),
    ("80053", "Comprehensive metabolic panel", 699, "CLINICAL LABORATORY", "Ancillary", "Laboratory"),
    ("85025", "Complete blood count with differential", 699, "CLINICAL LABORATORY", "Ancillary", "Laboratory"),
    ("93000", "Electrocardiogram, complete", 412, "LEVEL I CARDIOGRAM", "Ancillary", "Cardiology"),
    ("93005", "Electrocardiogram, tracing only", 412, "LEVEL I CARDIOGRAM", "Ancillary", "Cardiology"),
    ("J1100", "Dexamethasone injection, up to 1mg", 471, "LEVEL I DRUG", "Ancillary", "Drugs"),
    ("J2270", "Morphine sulfate injection", 472, "LEVEL II DRUG", "Ancillary", "Drugs"),
    ("G0008", "Administration of influenza vaccine", 459, "VACCINE ADMINISTRATION", "Ancillary", "Preventive Medicine and Related Services"),
    ("G0009", "Administration of pneumococcal vaccine", 459, "VACCINE ADMINISTRATION", "Ancillary", "Preventive Medicine and Related Services"),
    ("0001A", "COVID-19 vaccine administration, first dose", 459, "VACCINE ADMINISTRATION", "Ancillary", "Preventive Medicine and Related Services"),
    ("36415", "Collection of venous blood by venipuncture", 493, "BLOOD DRAW", "Incidental", "Incidental procedures and services"),
    ("90837", "Psychotherapy, 60 minutes", 603, "LEVEL II PSYCHIATRIC THERAPY", "Significant Procedure", "Mental health"),
    ("90834", "Psychotherapy, 45 minutes", 602, "LEVEL I PSYCHIATRIC THERAPY", "Significant Procedure", "Mental health"),
]


# (dx_code, description, eapg, eapg_desc, eapg_type, eapg_category)
ICD10 = [
    ("E119", "Type 2 diabetes mellitus without complications", 415, "DIABETES AND MINOR ENDOCRINE DIAGNOSES", "Medical Visit", "Diseases and disorders of endocrine system"),
    ("I10",  "Essential hypertension", 413, "HYPERTENSION AND CARDIAC CATH DIAGNOSES", "Medical Visit", "Diseases and disorders of circulatory system"),
    ("J449", "Chronic obstructive pulmonary disease, unspecified", 423, "RESPIRATORY DIAGNOSES", "Medical Visit", "Diseases and disorders of respiratory system"),
    ("L821", "Other seborrheic keratosis", 511, "SKIN DIAGNOSES", "Medical Visit", "Diseases and disorders of skin"),
    ("F329", "Major depressive disorder, single episode, unspecified", 619, "DEPRESSION AND ANXIETY DIAGNOSES", "Medical Visit", "Mental diseases and disorders"),
    ("F419", "Anxiety disorder, unspecified", 619, "DEPRESSION AND ANXIETY DIAGNOSES", "Medical Visit", "Mental diseases and disorders"),
    ("K219", "Gastroesophageal reflux disease", 431, "DIGESTIVE DIAGNOSES", "Medical Visit", "Digestive system"),
    ("M545", "Low back pain", 501, "MUSCULOSKELETAL DIAGNOSES", "Medical Visit", "Musculoskeletal system"),
    ("Z0000", "General adult examination without abnormal findings", 491, "MEDICAL VISIT INDICATOR", "Medical Visit", "Preventive medicine"),
    ("N390", "Urinary tract infection, site not specified", 445, "GENITOURINARY DIAGNOSES", "Medical Visit", "Genitourinary system"),
]


# EAPG weights keyed by APG number. For synthetic data we use one effective
# date (2016-04-01) and leave final_rate empty, so every recent DOS resolves
# to the same weight.
EAPG_WEIGHTS = {
    271: "5.2500",  321: "2.4000",  322: "4.1200",  323: "6.8900",
    412: "0.6400",  413: "0.0000",  415: "0.0000",  423: "0.0000",
    431: "0.0000",  445: "0.0000",  451: "3.2500",  452: "5.1000",
    459: "0.5800",  471: "0.4500",  472: "0.7200",  491: "0.0000",
    493: "0.0000",  501: "0.0000",  511: "0.0000",  602: "1.8500",
    603: "2.4100",  619: "0.0000",  699: "0.3500",
}


# (source, peer_group, cure_code, base_rate_code, blend_rate_code, capital_rate_code,
#  region, effective_date, rate)
BASE_RATES = [
    ("dtc", "Clinic*",  None, "1407", None, None, "Downstate", date(2015, 4, 1), "169.02"),
    ("dtc", "Clinic*",  None, "1407", None, None, "Upstate",   date(2015, 4, 1), "150.40"),
    ("dtc", "Amb Surg", None, "1408", None, None, "Downstate", date(2015, 4, 1), "210.50"),
    ("dtc", "Amb Surg", None, "1408", None, None, "Upstate",   date(2015, 4, 1), "187.25"),
    ("dtc", "SBHC*",    None, "1409", None, None, "Downstate", date(2015, 4, 1), "142.00"),
    ("dtc", "SBHC*",    None, "1409", None, None, "Upstate",   date(2015, 4, 1), "125.00"),
    ("hospital", "Clinic*",                      None, "1407", None, None, "Downstate", date(2012, 5, 1), "183.53"),
    ("hospital", "Clinic*",                      None, "1407", None, None, "Upstate",   date(2012, 5, 1), "165.20"),
    ("hospital", "Emergency Department (ED)",    None, "1434", None, None, "Downstate", date(2012, 5, 1), "245.00"),
    ("hospital", "Emergency Department (ED)",    None, "1434", None, None, "Upstate",   date(2012, 5, 1), "212.00"),
]


NY_COUNTIES = [
    ("ALBANY", 1, "Phase 3", "Upstate"),        ("ALLEGANY", 2, "Phase 3", "Upstate"),
    ("BRONX", 58, "Phase 1", "Downstate"),      ("BROOME", 4, "Phase 2", "Upstate"),
    ("CATTARAUGUS", 5, "Phase 3", "Upstate"),   ("CAYUGA", 6, "Phase 3", "Upstate"),
    ("CHAUTAUQUA", 7, "Phase 3", "Upstate"),    ("CHEMUNG", 8, "Phase 3", "Upstate"),
    ("CHENANGO", 9, "Phase 3", "Upstate"),      ("CLINTON", 10, "Phase 3", "Upstate"),
    ("COLUMBIA", 11, "Phase 3", "Upstate"),     ("CORTLAND", 12, "Phase 3", "Upstate"),
    ("DELAWARE", 13, "Phase 3", "Upstate"),     ("DUTCHESS", 14, "Phase 2", "Downstate"),
    ("ERIE", 15, "Phase 2", "Upstate"),         ("ESSEX", 16, "Phase 3", "Upstate"),
    ("FRANKLIN", 17, "Phase 3", "Upstate"),     ("FULTON", 18, "Phase 3", "Upstate"),
    ("GENESEE", 19, "Phase 3", "Upstate"),      ("GREENE", 20, "Phase 3", "Upstate"),
    ("HAMILTON", 21, "Phase 3", "Upstate"),     ("HERKIMER", 22, "Phase 3", "Upstate"),
    ("JEFFERSON", 23, "Phase 3", "Upstate"),    ("KINGS", 24, "Phase 1", "Downstate"),
    ("LEWIS", 25, "Phase 3", "Upstate"),        ("LIVINGSTON", 26, "Phase 3", "Upstate"),
    ("MADISON", 27, "Phase 3", "Upstate"),      ("MONROE", 40, "Phase 2", "Upstate"),
    ("MONTGOMERY", 39, "Phase 3", "Upstate"),   ("NASSAU", 28, "Phase 1", "Downstate"),
    ("NEW YORK", 29, "Phase 1", "Downstate"),   ("NIAGARA", 30, "Phase 2", "Upstate"),
    ("ONEIDA", 31, "Phase 2", "Upstate"),       ("ONONDAGA", 32, "Phase 2", "Upstate"),
    ("ONTARIO", 33, "Phase 3", "Upstate"),      ("ORANGE", 34, "Phase 2", "Downstate"),
    ("ORLEANS", 35, "Phase 3", "Upstate"),      ("OSWEGO", 36, "Phase 3", "Upstate"),
    ("OTSEGO", 37, "Phase 3", "Upstate"),       ("PUTNAM", 38, "Phase 2", "Downstate"),
    ("QUEENS", 61, "Phase 2", "Downstate"),     ("RENSSELAER", 59, "Phase 3", "Upstate"),
    ("RICHMOND", 60, "Phase 1", "Downstate"),   ("ROCKLAND", 41, "Phase 2", "Downstate"),
    ("SARATOGA", 42, "Phase 3", "Upstate"),     ("SCHENECTADY", 43, "Phase 3", "Upstate"),
    ("SCHOHARIE", 44, "Phase 3", "Upstate"),    ("SCHUYLER", 45, "Phase 3", "Upstate"),
    ("SENECA", 46, "Phase 3", "Upstate"),       ("STEUBEN", 3, "Phase 3", "Upstate"),
    ("ST LAWRENCE", 62, "Phase 3", "Upstate"),  ("SUFFOLK", 47, "Phase 2", "Downstate"),
    ("SULLIVAN", 48, "Phase 3", "Upstate"),     ("TIOGA", 49, "Phase 3", "Upstate"),
    ("TOMPKINS", 50, "Phase 3", "Upstate"),     ("ULSTER", 51, "Phase 3", "Upstate"),
    ("WARREN", 52, "Phase 3", "Upstate"),       ("WASHINGTON", 53, "Phase 3", "Upstate"),
    ("WAYNE", 54, "Phase 3", "Upstate"),        ("WESTCHESTER", 55, "Phase 2", "Downstate"),
    ("WYOMING", 56, "Phase 3", "Upstate"),      ("YATES", 57, "Phase 3", "Upstate"),
]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


EFF = date(2016, 4, 1)


async def run() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as s:
        # Reset only the reference tables; do NOT touch users, audit, or claims.
        for cls in (HcpcsToEapg, Icd10ToEapg, ApgWeight, ApgBaseRate, ProviderCounty):
            await s.execute(delete(cls))

        # HCPCS
        for code, desc, eapg, eapg_desc, eapg_type, category in HCPCS:
            s.add(HcpcsToEapg(
                hcpcs=code, description=desc, eapg=eapg, eapg_desc=eapg_desc,
                eapg_type=eapg_type, eapg_category=category,
                eapg_service_line="99", quarter_effective_date=EFF,
            ))

        # ICD-10
        for dx, desc, eapg, eapg_desc, eapg_type, category in ICD10:
            s.add(Icd10ToEapg(
                dx_code=dx, description=desc, gender="0", eapg=eapg,
                eapg_desc=eapg_desc, eapg_type=eapg_type, eapg_category=category,
                eapg_service_line="99", effective_date=EFF,
            ))

        # Weights
        for apg, weight_str in EAPG_WEIGHTS.items():
            s.add(ApgWeight(
                apg=apg, apg_description=f"Synthetic APG {apg}",
                effective_date=EFF, weight=Decimal(weight_str),
                is_final_rate=False, year_rate=None,
            ))

        # Base rates
        for source, peer, cure, base, blend, cap, region, eff, rate in BASE_RATES:
            s.add(ApgBaseRate(
                source=source, peer_group=peer, cure_code=cure,
                base_rate_code=base, blend_rate_code=blend, capital_rate_code=cap,
                region=region, effective_date=eff, rate=Decimal(rate),
            ))

        # Counties
        for name, code, phase, region in NY_COUNTIES:
            s.add(ProviderCounty(
                county_code=code, county_name=name,
                health_home_phase=phase, region=region,
            ))

        await s.commit()

    log.info("Seeded synthetic reference data:")
    log.info("  %d HCPCS, %d ICD-10, %d weights, %d base rates, %d counties",
             len(HCPCS), len(ICD10), len(EAPG_WEIGHTS), len(BASE_RATES), len(NY_COUNTIES))
    log.info("Note: these are FABRICATED values for demo/testing only.")
    log.info("Run `python -m backend.db.init_db --workbook <xlsx>` to load real NYS DOH data.")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
