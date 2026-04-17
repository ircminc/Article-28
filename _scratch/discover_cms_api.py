"""Throwaway discovery script — NOT part of the app.

Purpose: figure out what the current CMS Physician Fee Schedule dataset is
called in the data.json catalog, what its UUID is, and what fields it has
(HCPCS_CD? HCPCS? LOCALITY? LOCALITY_NUM?).

Run in a Codespace terminal:
    python _scratch/discover_cms_api.py > cms_discovery.txt

Then paste cms_discovery.txt back in chat.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request


CATALOG_URL = "https://data.cms.gov/data.json"
KEYWORDS = ("physician fee schedule", "pfs", "mpfs")


def http_get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "APG-Analyzer/discovery"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    print(f"Fetching catalog: {CATALOG_URL}")
    catalog = http_get_json(CATALOG_URL)
    datasets = catalog.get("dataset", [])
    print(f"Total datasets in catalog: {len(datasets)}")
    print()

    # Find candidate PFS / MPFS datasets
    matches = []
    for ds in datasets:
        title = (ds.get("title") or "").lower()
        desc = (ds.get("description") or "").lower()[:200]
        hay = f"{title} {desc}"
        if any(kw in hay for kw in KEYWORDS):
            matches.append(ds)

    print(f"== {len(matches)} dataset(s) matching physician fee schedule / PFS / MPFS ==")
    print()

    for i, ds in enumerate(matches, 1):
        print(f"--- Match #{i} ---")
        print(f"Title:       {ds.get('title')}")
        print(f"Identifier:  {ds.get('identifier')}")
        print(f"Description: {(ds.get('description') or '')[:200]}")
        modified = ds.get("modified")
        if modified:
            print(f"Modified:    {modified}")
        distributions = ds.get("distribution", [])
        print(f"Distributions: {len(distributions)}")
        # Show the 'latest' API distribution if present
        for dist in distributions:
            if dist.get("description") == "latest" and dist.get("format") == "API":
                access_url = dist.get("accessURL", "")
                print(f"  LATEST API URL: {access_url}")
                # Extract the UUID from the URL
                m = re.search(r"/dataset/([0-9a-f-]{36})/", access_url or "")
                if m:
                    uuid = m.group(1)
                    print(f"  LATEST UUID:    {uuid}")
                    # Fetch a sample row to discover field names
                    sample_url = f"{access_url}?size=1"
                    print(f"  Sampling one row from: {sample_url}")
                    try:
                        sample = http_get_json(sample_url)
                    except Exception as e:
                        print(f"  ERROR sampling: {e}")
                    else:
                        if isinstance(sample, list) and sample:
                            fields = list(sample[0].keys())
                            print(f"  Field names ({len(fields)}): {fields}")
                            print(f"  First row sample:")
                            for k, v in list(sample[0].items())[:20]:
                                print(f"    {k} = {v!r}")
                        else:
                            print(f"  Sample returned: {sample!r}")
                break
        print()

    # If we found candidates, try the first one with our actual query (HCPCS 99213)
    if matches:
        first = matches[0]
        for dist in first.get("distribution", []):
            if dist.get("description") == "latest" and dist.get("format") == "API":
                access_url = dist.get("accessURL", "")
                print("=" * 70)
                print(f"Trying a real HCPCS lookup against: {first.get('title')}")
                print(f"Query: 99213, locality 01")
                print("=" * 70)
                # Try several field name candidates
                for hcpcs_field in ("HCPCS_CD", "HCPCS_CODE", "HCPCS", "PROC_CODE"):
                    for locality_field in ("LOCALITY_NUM", "LOCALITY", "LOCALITY_CD", "MAC_LOCALITY"):
                        qs = urllib.parse.urlencode({
                            f"filter[{hcpcs_field}]": "99213",
                            f"filter[{locality_field}]": "01",
                            "size": "3",
                        })
                        test_url = f"{access_url}?{qs}"
                        try:
                            result = http_get_json(test_url)
                        except Exception as e:
                            continue
                        if isinstance(result, list) and result:
                            print(f"  ✓ HIT with filters {hcpcs_field} + {locality_field}")
                            print(f"  URL: {test_url}")
                            print(f"  Returned {len(result)} row(s), first row:")
                            for k, v in list(result[0].items())[:25]:
                                print(f"    {k} = {v!r}")
                            return
                print("  No combination of common field names returned data.")
                print("  → you'll need to inspect the full field list above and tell Claude the correct names.")
                break


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
