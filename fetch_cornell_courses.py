"""
Pulls course + section data from Cornell's public Class Roster API (v2.0)
and saves it locally so you can inspect the real JSON shape and start
building your data model / conflict checker against real data.

Docs: https://classes.cornell.edu/content/<TERM>/api-details
Base: https://classes.cornell.edu/api/2.0/

Roster codes: FA (Fall), WI (Winter), SP (Spring), SU (Summer) + 2-digit year
  e.g. "FA25" = Fall 2025, "SP26" = Spring 2026

Rate limit: the API asks for no more than 1 request/second, so this script
sleeps between calls. Run it once, cache the output, and build everything
else against the local JSON files instead of hitting the API repeatedly.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = "https://classes.cornell.edu/api/2.0"
OUTPUT_DIR = Path("cornell_data")
OUTPUT_DIR.mkdir(exist_ok=True)


def get_subjects(roster: str) -> list[dict]:
    """Returns all subjects (e.g. CS, MATH, GOVT) offered in a given roster."""
    resp = requests.get(
        f"{BASE_URL}/config/subjects.json",
        params={"roster": roster},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["subjects"]


def get_classes(roster: str, subject: str) -> list[dict]:
    """Returns all scheduled classes (with sections) for one subject."""
    resp = requests.get(
        f"{BASE_URL}/search/classes.json",
        params={"roster": roster, "subject": subject},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["classes"]


def fetch_all(roster: str, subject_codes: list[str] | None = None) -> None:
    """
    Fetches classes for the given subject codes (or ALL subjects if None)
    and writes one JSON file per subject into cornell_data/.
    """
    subjects = get_subjects(roster)
    subject_map = {s["value"]: s["descr"] for s in subjects}

    codes = subject_codes or list(subject_map.keys())
    print(f"Fetching {len(codes)} subject(s) for roster {roster}...")

    for i, code in enumerate(codes, start=1):
        print(f"  [{i}/{len(codes)}] {code} ({subject_map.get(code, '?')})")
        try:
            classes = get_classes(roster, code)
        except requests.HTTPError as e:
            print(f"    failed: {e}")
            continue

        out_path = OUTPUT_DIR / f"{roster}_{code}.json"
        out_path.write_text(json.dumps(classes, indent=2))
        print(f"    saved {len(classes)} course(s) -> {out_path}")

        time.sleep(1.1)  # stay under the 1 req/sec limit


if __name__ == "__main__":
    ROSTER = "FA26"  # change to the term you want, e.g. "SP26"

    # Start small: just a couple subjects so you can inspect the JSON shape
    # before pulling everything. Once you've looked at the structure, call
    # fetch_all(ROSTER) with no subject_codes argument to get all subjects.
    fetch_all(ROSTER, subject_codes=["CS", "MATH", "GOVT", "AEM"])

    # --- Inspect one course in detail ---
    sample = json.loads((OUTPUT_DIR / f"{ROSTER}_CS.json").read_text())
    print("\n--- First course raw structure ---")
    print(json.dumps(sample[0], indent=2)[:3000])