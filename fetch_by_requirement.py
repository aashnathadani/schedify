"""
Fetches every course that satisfies a distribution requirement (e.g. Liberal
Studies), across ALL subjects, in a single API call -- rather than fetching
subject-by-subject and filtering locally.
"""

import json
from pathlib import Path

import requests

BASE_URL = "https://classes.cornell.edu/api/2.0"
OUTPUT_DIR = Path("cornell_data")
OUTPUT_DIR.mkdir(exist_ok=True)


def fetch_by_distribution(roster: str, distr_codes: list[str], match_all: bool = False) -> list[dict]:
    params = [("roster", roster)]
    for code in distr_codes:
        params.append(("distrReqs[]", code))
    if match_all:
        params.append(("distrReqs-type", "all"))

    resp = requests.get(f"{BASE_URL}/search/classes.json", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]["classes"]


if __name__ == "__main__":
    ROSTER = "FA26"
    DISTR_CODES = ["SBA-AG"]

    classes = fetch_by_distribution(ROSTER, DISTR_CODES)
    print(f"Found {len(classes)} course(s) matching {DISTR_CODES}")

    out_path = OUTPUT_DIR / f"{ROSTER}_distr_{'_'.join(DISTR_CODES)}.json"
    out_path.write_text(json.dumps(classes, indent=2))
    print(f"Saved -> {out_path}")