"""v3 — CMS PFS uses DKAN. Try DKAN-style API endpoints on known UUIDs."""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request


# From v2 discovery, we know these UUIDs exist on pfs.data.cms.gov
# (they show up in the /data.json catalog). Now find the correct query URL.
TEST_UUIDS = {
    "Indicators for 2025": "1a4e7cb4-65db-48fd-8250-a64a3cc6e583",
    "Localities for 2025": "2c07a8fe-ac99-4ae6-855f-e6fe7597dc8b",
    "Indicators for 2024B": None,   # filled from catalog at runtime
    "Localities for 2024B": None,
}

# DKAN (Data Knowledge & Navigation) query URL patterns to try
ENDPOINT_PATTERNS = [
    # DKAN datastore query API (most common)
    "https://pfs.data.cms.gov/api/1/datastore/query/{uuid}?limit=1",
    "https://pfs.data.cms.gov/api/1/datastore/query/{uuid}/0?limit=1",
    # DKAN SQL endpoint (URL-encoded square brackets)
    "https://pfs.data.cms.gov/api/1/datastore/sql?query="
        + urllib.parse.quote("[SELECT * FROM " + "{uuid}" + "][LIMIT 1]"),
    # Resource ID instead of dataset ID
    "https://pfs.data.cms.gov/api/1/datastore/imports/{uuid}",
    # Metastore item
    "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/{uuid}",
    # Catalog-served full dataset URL
    "https://pfs.data.cms.gov/dataset/{uuid}",
]


def http_get(url: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "APG-Analyzer/discovery"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", errors="replace") if e.fp else str(e))
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def section(title):
    print(); print("=" * 72); print(title); print("=" * 72)


def try_metastore_item(uuid: str) -> dict | None:
    """Get the full metastore record for a dataset — often includes
    the 'distribution' with resource downloadable URLs and the resource UUIDs."""
    url = f"https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/{uuid}"
    status, body = http_get(url)
    print(f"  [{status}] metastore item {uuid}")
    if status != 200:
        print(f"    ↳ {body[:200]}")
        return None
    try:
        return json.loads(body)
    except Exception as e:
        print(f"    ↳ not JSON: {e}")
        return None


def try_every_endpoint(uuid: str, label: str):
    section(f"UUID: {label}  ({uuid})")
    for pattern in ENDPOINT_PATTERNS:
        url = pattern.format(uuid=uuid)
        status, body = http_get(url)
        first = body[:150].replace("\n", " ")
        flag = "✓" if status == 200 and body.startswith(("[", "{")) else " "
        print(f"  [{flag}{status}] {url[:95]}")
        print(f"          ↳ {first}")
        if flag == "✓":
            try:
                j = json.loads(body)
            except Exception:
                continue
            # Try to find rows — DKAN wraps them in different ways
            rows = None
            if isinstance(j, list):
                rows = j
            elif isinstance(j, dict):
                for key in ("results", "data", "items", "records"):
                    if key in j and isinstance(j[key], list):
                        rows = j[key]
                        break
                if rows is None and "result" in j:
                    r = j["result"]
                    if isinstance(r, dict):
                        for key in ("records", "data"):
                            if key in r and isinstance(r[key], list):
                                rows = r[key]
                                break
            if rows:
                print(f"    ✓✓✓ FOUND {len(rows)} row(s)")
                if isinstance(rows[0], dict):
                    print(f"    Field names: {list(rows[0].keys())}")
                    print(f"    First row:")
                    for k, v in list(rows[0].items())[:30]:
                        print(f"      {k} = {v!r}")
                else:
                    print(f"    First entry: {rows[0]!r}")
            else:
                # Maybe it's a metadata response — dump top-level keys
                print(f"    JSON top-level keys: {list(j.keys()) if isinstance(j, dict) else '(list)'}")


def main():
    # 1) Inspect the metastore for the Indicators-2025 dataset — may expose
    #    'distribution' array with resource UUIDs we need for the query API
    print("=== Step 1: metastore inspection ===")
    for label, uuid in TEST_UUIDS.items():
        if uuid is None:
            continue
        meta = try_metastore_item(uuid)
        if meta:
            print(f"  {label} metastore keys: {list(meta.keys())}")
            dist = meta.get("distribution", [])
            print(f"    {len(dist)} distribution(s):")
            for d in dist[:5]:
                print(f"      - keys: {list(d.keys())}")
                for k in ("identifier", "accessURL", "downloadURL", "@id", "%Ref:downloadURL", "format", "mediaType", "title", "describedBy"):
                    if k in d:
                        print(f"        {k}: {str(d[k])[:120]}")

    # 2) Try every known endpoint pattern against each known UUID
    print("\n=== Step 2: endpoint probes ===")
    for label, uuid in TEST_UUIDS.items():
        if uuid is None:
            continue
        try_every_endpoint(uuid, label)

    # 3) DKAN "datastore query" POST with JSON body — sometimes required
    print("\n=== Step 3: DKAN datastore query (POST) ===")
    uuid_2025 = TEST_UUIDS["Indicators for 2025"]
    url = f"https://pfs.data.cms.gov/api/1/datastore/query/{uuid_2025}"
    body = json.dumps({"limit": 1}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": "APG-Analyzer/discovery", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"  [{resp.status}] POST {url}")
            data = resp.read().decode("utf-8", errors="replace")
            print(f"    ↳ first 500: {data[:500]}")
    except Exception as e:
        print(f"  [?] POST {url} → {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
