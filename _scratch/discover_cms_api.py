"""v2 — CMS moved PFS data to pfs.data.cms.gov subdomain. Try that catalog."""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request


# Candidate catalog URLs — try each, look for any that returns a data.json payload.
CATALOG_CANDIDATES = [
    "https://pfs.data.cms.gov/data.json",
    "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items",
    "https://data.cms.gov/data.json",   # main catalog (already tested — no PFS)
]

# Known UUIDs from earlier web searches — use these to probe the API even
# without the catalog.
KNOWN_UUIDS = {
    "Indicators for 2025": "1a4e7cb4-65db-48fd-8250-a64a3cc6e583",
    "Indicators for 2024A": "b9841b4a-9811-41e2-ae5a-c00d51b19df1",
    "Localities for 2025": "2c07a8fe-ac99-4ae6-855f-e6fe7597dc8b",
    "Localities for 2023": "c4225a3a-4abe-40a4-bb8c-65dcd1ebe8e8",
}

# API base URLs to try per UUID
API_BASES = [
    "https://pfs.data.cms.gov/data-api/v1/dataset/{uuid}/data",
    "https://data.cms.gov/data-api/v1/dataset/{uuid}/data",
    "https://pfs.data.cms.gov/api/1/datastore/sql?query=[SELECT * FROM {uuid}][LIMIT 1]",
]


def http_get(url: str, timeout: int = 60) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "APG-Analyzer/discovery"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", errors="replace") if e.fp else str(e))
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def try_catalogs() -> list[dict]:
    """Return list of PFS-matching datasets found across catalog candidates."""
    all_matches: list[dict] = []
    for url in CATALOG_CANDIDATES:
        print_section(f"Trying catalog: {url}")
        status, body = http_get(url)
        print(f"HTTP {status}, body length {len(body)}")
        if status != 200:
            print(f"First 300 chars: {body[:300]}")
            continue
        try:
            data = json.loads(body)
        except Exception as e:
            print(f"Not JSON: {e}; first 300 chars: {body[:300]}")
            continue

        # Handle both data.json (has 'dataset' array) and metastore (array at top)
        datasets = data.get("dataset", data) if isinstance(data, dict) else data
        if not isinstance(datasets, list):
            print(f"Unexpected catalog shape (top keys: {list(data.keys()) if isinstance(data, dict) else '?'})")
            continue
        print(f"Catalog has {len(datasets)} dataset(s).")

        # Find "Pricing" / "Indicators" / "Localities" datasets for recent years
        for ds in datasets:
            title = (ds.get("title") or ds.get("name") or "").strip()
            if not title:
                continue
            tlow = title.lower()
            if any(k in tlow for k in ("pricing", "physician fee", "pfs", "mpfs", "indicators for", "localities for")):
                print(f"  MATCH: {title}")
                all_matches.append({**ds, "_catalog_url": url})

    return all_matches


def probe_uuid(uuid: str, label: str) -> None:
    """Try each API base URL with this UUID; report what works."""
    print_section(f"Probe UUID: {label}  ({uuid})")
    for base_tmpl in API_BASES:
        url = base_tmpl.format(uuid=uuid) + ("?size=1" if "?" not in base_tmpl else "&limit=1")
        status, body = http_get(url, timeout=30)
        short = body[:200].replace("\n", " ")
        print(f"  [{status}] {url[:90]}...")
        print(f"          ↳ {short}")
        if status == 200 and body.startswith(("[", "{")):
            try:
                j = json.loads(body)
                if isinstance(j, list) and j:
                    print(f"  ✓ WORKS. Field names: {list(j[0].keys())}")
                    print(f"    Sample row: {j[0]}")
                    return
                if isinstance(j, dict) and j.get("data"):
                    print(f"  ✓ WORKS (wrapped). Top-level keys: {list(j.keys())}")
                    if isinstance(j["data"], list) and j["data"]:
                        print(f"    Field names: {list(j['data'][0].keys())}")
                        print(f"    Sample row: {j['data'][0]}")
                    return
            except Exception as e:
                print(f"  (json parse: {e})")


def main() -> None:
    matches = try_catalogs()

    print()
    print_section(f"Summary: {len(matches)} catalog-listed match(es) across all sources")
    for m in matches[:20]:
        print(f"  {m.get('title')} — id={m.get('identifier') or m.get('id', '?')}")

    # Probe the known UUIDs regardless
    for label, uuid in KNOWN_UUIDS.items():
        probe_uuid(uuid, label)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
