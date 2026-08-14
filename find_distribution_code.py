"""
Helper: find the crseAttrValue code for a distribution requirement by
inspecting a course you already know satisfies it.

Usage: change SUBJECT/CATALOG_NBR below to a course you know fulfills
the category you're after (e.g. a Liberal Studies course), run this,
and read off the crseAttrValue + descr for the "Distribution Requirements"
attrDescr group.
"""

import json
from pathlib import Path

ROSTER = "FA26"
SUBJECT = "AEM"        # change to a subject you know satisfies your category
CATALOG_NBR = "2350"    # change to that course's catalog number

path = Path("cornell_data") / f"{ROSTER}_{SUBJECT}.json"
data = json.loads(path.read_text())
course = next(c for c in data if c["catalogNbr"] == CATALOG_NBR)

print(f"{SUBJECT} {CATALOG_NBR} attributes:\n")
for attr in course["crseAttrs"]:
    print(f"  code: {attr['crseAttrValue']!r:15} descr: {attr['descr']}  (group: {attr['attrDescr']})")