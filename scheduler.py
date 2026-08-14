"""
Core scheduling engine: turns the raw Cornell Class Roster JSON into a clean
data model, then generates all conflict-free schedules across chosen courses.

Courses and requirement categories are fetched from Cornell's live API
automatically the first time they're requested, and cached locally in
cornell_data/ so future lookups are instant.
"""

import json
from dataclasses import dataclass, field
from datetime import time, datetime, date, timedelta
from itertools import product
from pathlib import Path

import requests

DATA_DIR = Path("cornell_data")
DATA_DIR.mkdir(exist_ok=True)

BASE_URL = "https://classes.cornell.edu/api/2.0"

DAY_CODES = {"M": "Mon", "T": "Tue", "W": "Wed", "R": "Thu", "F": "Fri", "S": "Sat", "U": "Sun"}

# Academic career codes Cornell's API uses. UG = undergrad, GR = grad,
# LAW = law, MED/VET etc. also exist but are rare to run into here.
DEFAULT_CAREERS = {"UG"}


# ---------- Data model ----------

@dataclass
class Meeting:
    days: set[str]
    start: time | None
    end: time | None


@dataclass
class Section:
    course: str          # "CS 1110"
    component: str        # "LEC", "DIS", "LAB"
    section_id: str        # "001"
    class_nbr: int
    instructor: str
    meetings: list[Meeting]

    def __repr__(self):
        times = ", ".join(
            f"{''.join(sorted(m.days))} {m.start}-{m.end}" if m.start else "TBA"
            for m in self.meetings
        )
        return f"{self.course} {self.component} {self.section_id} ({times})"


@dataclass
class EnrollGroup:
    sections_by_component: dict[str, list[Section]]
    units_min: float
    units_max: float


@dataclass
class Course:
    code: str
    title: str
    enroll_groups: list[EnrollGroup]
    acad_career: str = ""   # "UG", "GR", "LAW", etc. -- "" means unknown/unspecified


@dataclass
class CourseOption:
    """One complete, valid way to take a course: one enrollGroup, with
    exactly one section chosen per required component, plus its credits."""
    course_code: str
    sections: tuple[Section, ...]
    credits: float

    def __repr__(self):
        lines = "\n".join(f"    {s}" for s in self.sections)
        return f"{self.course_code} ({self.credits} credits):\n{lines}"


# ---------- Parsing helpers ----------

def parse_days(pattern: str) -> set[str]:
    if not pattern:
        return set()
    return {DAY_CODES[ch] for ch in pattern if ch in DAY_CODES}


def parse_time(t: str) -> time | None:
    if not t:
        return None
    try:
        return datetime.strptime(t, "%I:%M%p").time()
    except ValueError:
        return None


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_course(raw_course: dict) -> Course:
    """Converts one raw course dict from the roster JSON into a Course object."""
    code = f"{raw_course['subject']} {raw_course['catalogNbr']}"
    enroll_groups = []

    for eg in raw_course["enrollGroups"]:
        sections_by_component: dict[str, list[Section]] = {}
        for cs in eg["classSections"]:
            component = cs["ssrComponent"]
            meetings = []
            instructor_name = ""
            for m in cs["meetings"]:
                meetings.append(
                    Meeting(
                        days=parse_days(m.get("pattern", "")),
                        start=parse_time(m.get("timeStart")),
                        end=parse_time(m.get("timeEnd")),
                    )
                )
                if m.get("instructors"):
                    first = m["instructors"][0]
                    instructor_name = f"{first.get('firstName', '')} {first.get('lastName', '')}".strip()

            section = Section(
                course=code,
                component=component,
                section_id=cs["section"],
                class_nbr=cs["classNbr"],
                instructor=instructor_name,
                meetings=meetings,
            )
            sections_by_component.setdefault(component, []).append(section)

        enroll_groups.append(
            EnrollGroup(
                sections_by_component=sections_by_component,
                units_min=_to_float(eg.get("unitsMinimum")),
                units_max=_to_float(eg.get("unitsMaximum")),
            )
        )

    # Cornell's roster API reports this per-enrollGroup (a course can in
    # rare cases be cross-listed across careers), but in practice it's
    # consistent across a course's enrollGroups, so we take it once at the
    # course level and fall back to "" (unknown) if it's missing entirely.
    acad_career = ""
    for eg in raw_course["enrollGroups"]:
        if eg.get("acadCareer"):
            acad_career = eg["acadCareer"]
            break
    if not acad_career:
        acad_career = raw_course.get("acadCareer", "")

    return Course(code=code, title=raw_course["titleLong"], enroll_groups=enroll_groups, acad_career=acad_career)


# ---------- Career filtering (undergrad vs. grad, etc.) ----------

def filter_by_career(courses: list[Course], careers: set[str] | None = DEFAULT_CAREERS) -> list[Course]:
    """
    Filters a course list down to the given academic career level(s), e.g.
    {'UG'} for undergrad-only (the default) or {'GR'} for grad-only.

    Pass careers=None (or an empty set) to disable filtering entirely and
    keep every course regardless of career.

    Courses with an unknown/missing acad_career are kept rather than
    dropped -- we'd rather show something we're unsure about than silently
    hide a course because the API didn't report a career for it.
    """
    if not careers:
        return courses
    return [c for c in courses if not c.acad_career or c.acad_career in careers]


# ---------- Fetching + caching (subjects) ----------

def _fetch_subject_raw(subject: str, roster: str) -> list[dict]:
    """Fetches every class in a subject from Cornell's live API and caches
    it locally, so future lookups for this subject are instant."""
    resp = requests.get(
        f"{BASE_URL}/search/classes.json",
        params={"roster": roster, "subject": subject},
        timeout=15,
    )
    resp.raise_for_status()
    classes = resp.json()["data"]["classes"]

    path = DATA_DIR / f"{roster}_{subject}.json"
    path.write_text(json.dumps(classes, indent=2))
    return classes


def _load_subject_raw(subject: str, roster: str) -> list[dict]:
    """Reads a subject's classes from the local cache, fetching + caching
    from the live API first if it isn't saved yet."""
    path = DATA_DIR / f"{roster}_{subject}.json"
    if path.exists():
        return json.loads(path.read_text())

    print(f"  (fetching {subject} from Cornell's API for the first time...)")
    return _fetch_subject_raw(subject, roster)


def load_course(subject: str, catalog_nbr: str, roster: str = "FA26") -> Course | None:
    """Loads one specific course (e.g. subject='CS', catalog_nbr='1110'),
    fetching + caching its subject automatically if needed.

    Deliberately NOT career-filtered: if you ask for a specific course by
    number, you get it regardless of career -- filtering only applies to
    pools of electives built automatically (distribution categories and
    the fallback pool), not to courses you explicitly named.
    """
    raw_classes = _load_subject_raw(subject.upper(), roster)

    raw_course = next(
        (c for c in raw_classes if c["catalogNbr"] == catalog_nbr), None
    )
    if raw_course is None:
        return None

    return _build_course(raw_course)


def load_courses_from_file(path: Path) -> list[Course]:
    """Loads EVERY course from a saved JSON file (e.g. a subject or
    distribution-category cache)."""
    raw_classes = json.loads(Path(path).read_text())
    return [_build_course(rc) for rc in raw_classes]


# ---------- Lightweight lookups for autocomplete ----------

def list_subjects(roster: str = "FA26") -> list[dict]:
    """
    Every subject code + description offered in a roster, e.g.
    {'value': 'CS', 'descr': 'Computer Science'}. Fetched live once per
    roster and cached locally -- this is what lets the UI suggest subject
    codes as someone types a required course.
    """
    path = DATA_DIR / f"{roster}_subjects.json"
    if path.exists():
        return json.loads(path.read_text())

    resp = requests.get(f"{BASE_URL}/config/subjects.json", params={"roster": roster}, timeout=15)
    resp.raise_for_status()
    subjects = resp.json()["data"]["subjects"]
    path.write_text(json.dumps(subjects, indent=2))
    return subjects


def list_courses_in_subject(subject: str, roster: str = "FA26") -> list[dict]:
    """
    Lightweight {code, catalog_nbr, title} for every course in a subject --
    used for autocomplete once someone's typed a valid subject code, without
    building full Course objects (which would parse every section just to
    list course numbers).
    """
    raw_classes = _load_subject_raw(subject.upper(), roster)
    return [
        {
            "code": f"{c['subject']} {c['catalogNbr']}",
            "catalog_nbr": c["catalogNbr"],
            "title": c["titleLong"],
        }
        for c in raw_classes
    ]


def list_distribution_codes(roster: str = "FA26") -> list[dict]:
    """
    Best-effort list of distribution/requirement codes for autocomplete.

    Cornell's API does NOT expose an endpoint that enumerates every valid
    code -- confirmed against the live API docs, the only /config/ methods
    that exist are rosters, acadCareers, acadGroups, classLevels, and
    subjects. There is no config/distributions.

    So instead, this combines two sources:
      1. KNOWN_DISTRIBUTION_CODES -- a static seed list transcribed from
         Cornell's public distribution-code documentation (college-by-college
         pages under courses.cornell.edu / catalog.cornell.edu). Colleges
         add or rename codes over time, so treat this as a helpful starting
         point, not a guarantee of completeness.
      2. distribution_codes_from_cache() -- codes that have actually shown
         up on courses already cached locally (every course's raw JSON
         carries its own distribution codes under crseAttrs, the same data
         find_distribution_code.py already reads). This is always accurate
         for whatever's been fetched, and the list grows the more subjects
         get pulled in.

    Either way, a code typed here -- whether it's in this list or not --
    still gets validated for real against the live search API when
    schedules are generated (see GET /api/distribution/{code} for an
    instant single-code check).
    """
    merged: dict[str, str] = dict(KNOWN_DISTRIBUTION_CODES)
    for entry in distribution_codes_from_cache(roster):
        if entry["descr"] or entry["value"] not in merged:
            merged[entry["value"]] = entry["descr"] or merged.get(entry["value"], "")
    return [{"value": code, "descr": descr} for code, descr in sorted(merged.items())]


def distribution_codes_from_cache(roster: str = "FA26") -> list[dict]:
    """
    Scans every course JSON file already cached locally for this roster and
    collects the distribution/requirement codes that actually appear on
    cached courses (each course's crseAttrs where attrDescr ==
    "Distribution Requirements" -- the same field find_distribution_code.py
    reads). Always accurate for whatever's been fetched; just doesn't know
    about codes on courses nobody's pulled from the API yet.
    """
    seen: dict[str, str] = {}
    for path in DATA_DIR.glob(f"{roster}_*.json"):
        try:
            raw_classes = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for c in raw_classes:
            for attr in c.get("crseAttrs", []):
                if attr.get("attrDescr") == "Distribution Requirements":
                    code = attr.get("crseAttrValue")
                    descr = attr.get("descr", "")
                    if code and (descr or code not in seen):
                        seen[code] = descr
    return [{"value": code, "descr": descr} for code, descr in seen.items()]


# Seed list transcribed from Cornell's public distribution-code
# documentation as of early 2026 (see list_distribution_codes docstring
# for sourcing/caveats -- this is best-effort, not authoritative).
KNOWN_DISTRIBUTION_CODES: dict[str, str] = {
    # College of Agriculture and Life Sciences (AG)
    "CA-AG": "Cultural Analysis (CALS)",
    "D-AG": "Human Diversity (CALS)",
    "FL-AG": "Foreign Language (CALS)",
    "HA-AG": "Historical Analysis (CALS)",
    "KCM-AG": "Knowledge, Cognition, and Moral Reasoning (CALS)",
    "LA-AG": "Literature and the Arts (CALS)",
    "SBA-AG": "Social and Behavioral Analysis (CALS)",
    "ORL-AG": "Oral Expression (CALS)",
    "WRT-AG": "Written Expression (CALS)",
    "BIO-AG": "Introductory Life Sciences/Biology (CALS)",
    "BIOLS-AG": "Life Sciences (CALS)",
    "BIONLS-AG": "Non-Lab Life Sciences (CALS)",
    "CHPH-AG": "Chemistry/Physics (CALS)",
    "MQL-AG": "Quantitative Literacy (CALS)",
    "OPHLS-AG": "Other Physical and Life Sciences (CALS)",
    "ETH-AG": "Ethics (CALS)",
    # College of Arts and Sciences (AS)
    "ALC-AS": "Arts, Literature & Culture (Arts & Sciences)",
    "HST-AS": "Historical Analysis (Arts & Sciences)",
    "GLC-AS": "Global Citizenship (Arts & Sciences)",
    "SCD-AS": "Social Difference (Arts & Sciences)",
    "SSC-AS": "Social Science (Arts & Sciences)",
    "SMR-AS": "Symbolic/Mathematical Reasoning (Arts & Sciences)",
    "SDS-AS": "Statistics/Data Science (Arts & Sciences)",
    "BIO-AS": "Biological Sciences (Arts & Sciences)",
    "PHS-AS": "Physical Sciences (Arts & Sciences)",
    "ETM-AS": "Entrepreneurship/Management (Arts & Sciences)",
    "FLOPI-AS": "Foreign Language/Oral Proficiency (Arts & Sciences)",
    # College of Human Ecology (HE)
    "HA-HE": "Historical Analysis (Human Ecology)",
    "LAD-HE": "Literature & the Arts (Human Ecology)",
    "CA-HE": "Cultural Analysis (Human Ecology)",
    "D-HE": "Human Diversity (Human Ecology)",
    "SBA-HE": "Social and Behavioral Analysis (Human Ecology)",
    "MQR-HE": "Mathematics/Quantitative Reasoning (Human Ecology)",
    "PBS-HE": "Physical/Biological Sciences (Human Ecology)",
    "KCM-HE": "Knowledge, Cognition, and Moral Reasoning (Human Ecology)",
    # College of Architecture, Art, and Planning (AAP)
    "LA-AAP": "Literature & the Arts (Architecture, Art & Planning)",
    "HA-AAP": "Historical Analysis (Architecture, Art & Planning)",
    "ALC-AAP": "Arts, Literature & Culture (Architecture, Art & Planning)",
    "CA-AAP": "Cultural Analysis (Architecture, Art & Planning)",
    "SBA-AAP": "Social and Behavioral Analysis (Architecture, Art & Planning)",
    "MQR-AAP": "Mathematics/Quantitative Reasoning (Architecture, Art & Planning)",
    "PBS-AAP": "Physical/Biological Sciences (Architecture, Art & Planning)",
    "FL-AAP": "Foreign Language (Architecture, Art & Planning)",
    "KCM-AAP": "Knowledge, Cognition, and Moral Reasoning (Architecture, Art & Planning)",
    # Nolan School (Hotel Administration, HA)
    "ALC-HA": "Arts, Literature & Culture (Nolan School)",
    "ETM-HA": "Entrepreneurship/Management (Nolan School)",
    "GLC-HA": "Global Citizenship (Nolan School)",
    "HST-HA": "Historical Analysis (Nolan School)",
    "SCD-HA": "Social Difference (Nolan School)",
    "SSC-HA": "Social Science (Nolan School)",
    "SDS-HA": "Statistics/Data Science (Nolan School)",
    "SMR-HA": "Symbolic/Mathematical Reasoning (Nolan School)",
    # Engineering (EN)
    "CE-EN": "Cultural Engagement (Engineering)",
}


# ---------- Fetching + caching (distribution/requirement categories) ----------

def _fetch_distribution_raw(distr_code: str, roster: str) -> list[dict]:
    """Fetches every course satisfying a distribution requirement code
    across ALL subjects, and caches it locally."""
    resp = requests.get(
        f"{BASE_URL}/search/classes.json",
        params={"roster": roster, "distrReqs[]": distr_code},
        timeout=30,
    )
    resp.raise_for_status()
    classes = resp.json()["data"]["classes"]

    path = DATA_DIR / f"{roster}_distr_{distr_code}.json"
    path.write_text(json.dumps(classes, indent=2))
    return classes


def load_courses_by_distribution(
    distr_code: str,
    roster: str = "FA26",
    careers: set[str] | None = DEFAULT_CAREERS,
) -> list[Course]:
    """
    Loads every course satisfying a distribution requirement code (e.g.
    'CA-AG'), fetching + caching from the live API automatically if it
    isn't saved locally yet. This is what makes "any category" work
    without a separate manual fetch step.

    careers filters the pool by academic career (default: undergrad-only).
    Pass careers=None to get every course regardless of career.
    """
    path = DATA_DIR / f"{roster}_distr_{distr_code}.json"

    if path.exists():
        raw_classes = json.loads(path.read_text())
    else:
        print(f"  (fetching category '{distr_code}' from Cornell's API for the first time...)")
        raw_classes = _fetch_distribution_raw(distr_code, roster)

    courses = [_build_course(rc) for rc in raw_classes]
    return filter_by_career(courses, careers)


# ---------- Conflict checking ----------

def sections_conflict(a: Section, b: Section, buffer_minutes: int = 0) -> bool:
    """
    Two sections conflict if their meetings overlap in time on a shared day.
    buffer_minutes extends each meeting's effective end time, so back-to-back
    classes with too little gap (e.g. not enough time to walk across campus)
    are also treated as conflicts, not just literal time overlaps.
    """
    buffer = timedelta(minutes=buffer_minutes)
    for m1 in a.meetings:
        for m2 in b.meetings:
            if not (m1.days & m2.days):
                continue
            if m1.start is None or m1.end is None or m2.start is None or m2.end is None:
                continue  # times TBA -- can't verify, assume ok

            today = date.today()
            start1 = datetime.combine(today, m1.start)
            end1 = datetime.combine(today, m1.end) + buffer
            start2 = datetime.combine(today, m2.start)
            end2 = datetime.combine(today, m2.end) + buffer

            if start1 < end2 and start2 < end1:
                return True
    return False


def option_conflicts(opt_a: CourseOption, opt_b: CourseOption, buffer_minutes: int = 0) -> bool:
    return any(
        sections_conflict(s1, s2, buffer_minutes=buffer_minutes)
        for s1 in opt_a.sections
        for s2 in opt_b.sections
    )


# ---------- Generating valid per-course options ----------

def enroll_group_options(course: Course):
    """
    Yields every valid full selection for one course as a CourseOption:
    one enrollGroup, with exactly one section chosen per required component,
    carrying that enrollGroup's credit value along with it.
    """
    for eg in course.enroll_groups:
        component_lists = list(eg.sections_by_component.values())
        credits = eg.units_max if eg.units_max else eg.units_min
        for combo in product(*component_lists):
            yield CourseOption(course_code=course.code, sections=combo, credits=credits)


# ---------- Backtracking schedule generator ----------

def generate_schedules(courses: list[Course], max_results: int = 20, buffer_minutes: int = 0):
    """
    Returns up to max_results full schedules. Each schedule is a list of
    CourseOptions (one per course). buffer_minutes adds a minimum gap
    requirement between back-to-back classes (e.g. walking time).
    """
    all_options = [list(enroll_group_options(c)) for c in courses]

    for i, opts in enumerate(all_options):
        if not opts:
            print(f"Warning: {courses[i].code} has no valid section combos.")

    results = []

    def backtrack(i, chosen):
        if len(results) >= max_results:
            return
        if i == len(all_options):
            results.append(list(chosen))
            return
        for opt in all_options[i]:
            if all(not option_conflicts(opt, prev, buffer_minutes=buffer_minutes) for prev in chosen):
                chosen.append(opt)
                backtrack(i + 1, chosen)
                chosen.pop()

    backtrack(0, [])
    return results


# ---------- Demo ----------

if __name__ == "__main__":
    cs1110 = load_course("CS", "1110")
    math1920 = load_course("MATH", "1920")

    courses = [c for c in [cs1110, math1920] if c is not None]
    schedules = generate_schedules(courses, max_results=10, buffer_minutes=15)

    print(f"\nFound {len(schedules)} conflict-free schedule(s):\n")
    for i, schedule in enumerate(schedules, start=1):
        print(f"--- Schedule {i} ---")
        total_credits = sum(opt.credits for opt in schedule)
        for opt in schedule:
            print(opt)
        print(f"  Total credits: {total_credits}")
        print()