"""
Preference layer on top of scheduler.py.

Handles, all via typed input:
  - required courses (must appear in every schedule)
  - multiple requirement categories at once (e.g. "1 from CA-AG AND 1 from SBA-AG")
  - if no categories given, auto-fills remaining credits from cached courses
  - blocked time windows (hard "no classes during this time" constraint)
  - credit range (min/max total credits)
  - minimum gap between back-to-back classes (walking/commute buffer)
  - undergrad vs. grad filtering on every auto-built elective pool
  - diagnostics when zero schedules are found, explaining why
"""

from itertools import combinations
from dataclasses import dataclass
from datetime import time, datetime
import math
import random

from pathlib import Path

from scheduler import (
    Course,
    CourseOption,
    Section,
    DEFAULT_CAREERS,
    load_course,
    load_courses_from_file,
    load_courses_by_distribution,
    enroll_group_options,
    option_conflicts,
    filter_by_career,
)


# ---------- Blocked time windows ----------

def parse_clock(t: str) -> time:
    """Parses a plain time string like '10:00AM' into a time object."""
    return datetime.strptime(t, "%I:%M%p").time()


class BlockedWindow:
    """A hard 'no classes during this time' rule, e.g. no classes on
    Mon/Wed before 10am."""

    def __init__(self, days: list[str], start: str, end: str):
        self.days = set(days)  # e.g. {"Mon", "Wed"}
        self.start = parse_clock(start)
        self.end = parse_clock(end)


def section_hits_blocked_window(section: Section, blocked: list[BlockedWindow]) -> bool:
    for meeting in section.meetings:
        if meeting.start is None or meeting.end is None:
            continue
        for window in blocked:
            if not (meeting.days & window.days):
                continue
            if meeting.start < window.end and window.start < meeting.end:
                return True
    return False


def option_hits_blocked_window(option: CourseOption, blocked: list[BlockedWindow]) -> bool:
    return any(section_hits_blocked_window(s, blocked) for s in option.sections)


# ---------- Preference-aware option generation ----------

def valid_options_for_course(course: Course, blocked: list[BlockedWindow]):
    """Like enroll_group_options, but pre-filters out anything that hits
    a blocked time window -- so bad options never even enter the search."""
    return [
        opt for opt in enroll_group_options(course)
        if not option_hits_blocked_window(opt, blocked)
    ]


def generate_schedules_filtered(
    courses: list[Course],
    blocked: list[BlockedWindow],
    max_results: int = 50,
    buffer_minutes: int = 0,
):
    all_options = [valid_options_for_course(c, blocked) for c in courses]

    for i, opts in enumerate(all_options):
        if not opts:
            print(f"Warning: {courses[i].code} has no options left after filtering.")

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


def print_schedule(entry: dict):
    electives_str = ", ".join(
        f"{e['code']} (fulfills {e['requirement']})" for e in entry["electives_chosen"]
    ) or "(none)"
    print(f"Electives: {electives_str}")
    for opt in entry["schedule"]:
        print(opt)
    print(f"  Total credits: {entry['total_credits']}")
    print()


# ---------- Multiple requirement categories at once ----------

@dataclass
class RequirementGroup:
    """One requirement to fill, e.g. '1 course from the CA-AG pool' or
    '2 courses from the SBA-AG pool'. Give it a readable name, the pool of
    candidate courses, and how many of them are needed."""
    name: str
    pool: list[Course]
    num_needed: int


def _find_unavoidable_required_conflicts(required: list[Course]) -> list[tuple[str, str]]:
    """
    Checks each pair of required courses on their own -- ignoring blocked
    time windows and the walking buffer entirely -- to see whether EVERY
    combination of their sections conflicts. If so, there is no possible
    schedule that includes both of them, no matter what electives or time
    preferences are chosen, so we can name the exact pair to the user
    instead of a generic "couldn't find anything" message.
    """
    conflicting_pairs = []
    for c1, c2 in combinations(required, 2):
        opts1 = list(enroll_group_options(c1))
        opts2 = list(enroll_group_options(c2))
        if not opts1 or not opts2:
            continue
        if all(option_conflicts(o1, o2) for o1 in opts1 for o2 in opts2):
            conflicting_pairs.append((c1.code, c2.code))
    return conflicting_pairs


def plan_schedules_multi(
    required: list[Course],
    requirement_groups: list[RequirementGroup],
    blocked: list[BlockedWindow],
    max_credits: float | None = None,
    min_credits: float | None = None,
    max_results: int = 50,
    max_per_combo: int = 1,
    buffer_minutes: int = 0,
    max_attempts: int = 3000,
    seed: int | None = None,
):
    """
    Handles several requirement categories at once. Samples RANDOM
    combinations across all groups together, so diversity holds regardless
    of group count/size. Tracks credit totals of every conflict-free
    schedule it finds (even ones outside the credit range) so it can give
    a useful diagnostic if the final result set ends up empty.
    """
    rng = random.Random(seed)

    cleaned_groups = []
    for group in requirement_groups:
        usable = [c for c in group.pool if valid_options_for_course(c, blocked)]
        cleaned_groups.append(RequirementGroup(name=group.name, pool=usable, num_needed=group.num_needed))

    group_choices = [
        list(combinations(group.pool, group.num_needed))
        for group in cleaned_groups
    ]

    if any(len(choices) == 0 for choices in group_choices):
        empty = [g.name for g, c in zip(cleaned_groups, group_choices) if not c]
        print(f"No valid picks possible for group(s): {', '.join(empty)}")
        return []

    all_results = []
    seen_combos = set()
    attempts = 0

    # Diagnostics: track every conflict-free schedule's credit total, even
    # ones filtered out by the credit range, so we can explain a 0-result run.
    all_conflict_free_credit_totals = []

    while len(all_results) < max_results and attempts < max_attempts:
        attempts += 1

        cross_combo: tuple[tuple[Course, ...], ...] = (
            tuple(rng.choice(choices) for choices in group_choices) if group_choices else ()
        )
        combo_key = tuple(c.code for group_pick in cross_combo for c in group_pick)
        if combo_key in seen_combos:
            if group_choices:
                continue
        seen_combos.add(combo_key)

        # Pair each elective with the name of the requirement group it's
        # filling, e.g. (CS 2112, "CA-AG"), so the API/UI can later show
        # "CS 2112 (fulfills CA-AG)" instead of just a bare course code.
        electives_with_group = [
            (course, group.name)
            for group, group_pick in zip(cleaned_groups, cross_combo)
            for course in group_pick
        ]
        electives_flat = [course for course, _ in electives_with_group]
        course_set = required + electives_flat
        schedules = generate_schedules_filtered(
            course_set, blocked, max_results=max_per_combo * 5, buffer_minutes=buffer_minutes
        )

        added_for_this_combo = 0
        for schedule in schedules:
            total_credits = sum(opt.credits for opt in schedule)
            all_conflict_free_credit_totals.append(total_credits)

            if len(all_results) >= max_results:
                break
            if max_credits is not None and total_credits > max_credits:
                continue
            if min_credits is not None and total_credits < min_credits:
                continue

            all_results.append({
                "electives_chosen": [
                    {"code": c.code, "title": c.title, "requirement": req_name}
                    for c, req_name in electives_with_group
                ],
                "required_chosen": [
                    {"code": c.code, "title": c.title} for c in required
                ],
                "schedule": schedule,
                "total_credits": total_credits,
            })
            added_for_this_combo += 1

            if added_for_this_combo >= max_per_combo:
                break

        if not group_choices:
            # No elective groups at all -- only one possible course_set, so
            # there's nothing more to search after the first pass.
            break

    if not all_results:
        print("\nNo schedules found matching all your criteria.")
        if not all_conflict_free_credit_totals:
            conflicting_pairs = _find_unavoidable_required_conflicts(required)
            if conflicting_pairs:
                pairs_str = "; ".join(f"{a} and {b}" for a, b in conflicting_pairs)
                print(
                    f"Reason: {pairs_str} can't be taken together -- every "
                    f"available section of one overlaps with every available "
                    f"section of the other, regardless of electives, blocked "
                    f"times, or buffer. You'll need to drop one of these "
                    f"required courses or pick a different one to replace it."
                )
            else:
                print(
                    "Reason: your required courses don't conflict with each "
                    "other on their own, but no combination of them (plus any "
                    "electives) fits once your blocked time windows and "
                    "walking buffer are applied. Try loosening one of those "
                    "constraints."
                )
        else:
            lo, hi = min(all_conflict_free_credit_totals), max(all_conflict_free_credit_totals)
            print(
                f"Reason: conflict-free schedules DO exist, but their credit totals "
                f"ranged from {lo} to {hi}, which falls outside your requested "
                f"range of {min_credits}-{max_credits}. Try widening the range."
            )

    return all_results


# ---------- Input helpers ----------

def ask_required_courses(roster: str = "FA26") -> list[Course]:
    """
    Comma-separated required courses, e.g. 'CS 2110, MATH 2940'. Any subject
    not already cached locally is fetched from Cornell's live API
    automatically the first time it's needed.
    """
    raw = input(
        "Enter required courses, comma-separated (e.g. CS 2110, MATH 2940): "
    ).strip()

    entries = [e.strip() for e in raw.split(",") if e.strip()]
    courses = []

    for entry in entries:
        parts = entry.split()
        if len(parts) < 2:
            print(f"  Couldn't parse '{entry}' (expected e.g. 'CS 2110') -- skipping.")
            continue

        subject, catalog_nbr = parts[0].upper(), parts[1]
        course = load_course(subject, catalog_nbr, roster=roster)

        if course is None:
            print(f"  Couldn't find {subject} {catalog_nbr} in the {roster} roster -- skipping.")
            continue

        courses.append(course)
        print(f"  Added: {course.code} -- {course.title}")

    return courses


def ask_requirement_groups(roster: str = "FA26", careers: set[str] | None = DEFAULT_CAREERS) -> list[RequirementGroup]:
    """
    Lets the user add one or more distribution/requirement categories,
    e.g. 'CA-AG' needing 1 course. Categories are fetched from Cornell's
    live API automatically the first time they're used, and cached locally
    after that -- no separate manual fetch step needed.

    careers filters each category's pool by academic career (default:
    undergrad-only). Pass careers=None to include every career level.
    """
    groups = []
    print("\nAdd requirement categories to fill (leave blank when done).")
    print("(You can find a category's code using find_distribution_code.py.)")

    while True:
        code = input("  Distribution code (e.g. CA-AG) or blank to finish: ").strip().upper()
        if code == "":
            break

        pool = load_courses_by_distribution(code, roster=roster, careers=careers)
        if not pool:
            print(f"  No courses found for '{code}' -- double check the code and try again.")
            continue

        num_raw = input(f"  How many courses needed from {code}? [default 1]: ").strip()
        try:
            num_needed = int(num_raw) if num_raw else 1
        except ValueError:
            print("  Didn't understand that, using 1.")
            num_needed = 1

        groups.append(RequirementGroup(name=code, pool=pool, num_needed=num_needed))
        print(f"  Added group '{code}': {len(pool)} candidate course(s), need {num_needed}.")

    return groups


def ask_credit_range(default_min: float = 12, default_max: float = 18) -> tuple[float, float]:
    raw_min = input(f"Minimum total credits [default {default_min}]: ").strip()
    raw_max = input(f"Maximum total credits [default {default_max}]: ").strip()

    try:
        min_credits = float(raw_min) if raw_min else default_min
    except ValueError:
        print(f"Didn't understand that, using default of {default_min}.")
        min_credits = default_min

    try:
        max_credits = float(raw_max) if raw_max else default_max
    except ValueError:
        print(f"Didn't understand that, using default of {default_max}.")
        max_credits = default_max

    return min_credits, max_credits


def ask_buffer_minutes(default: int = 15) -> int:
    """Asks the user how much gap they want between back-to-back classes."""
    raw = input(
        f"Minimum minutes between back-to-back classes (walking time) "
        f"[default {default}]: "
    ).strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Didn't understand that, using default of {default} minutes.")
        return default


def ask_blocked_windows() -> list[BlockedWindow]:
    """Lets the user add one or more 'no classes during this time' rules."""
    blocked = []
    print("\nAdd times you don't want classes during (leave days blank when done).")
    print("Example: days 'Mon,Wed,Fri', start '12:00AM', end '9:00AM'.")

    while True:
        days_raw = input("  Days (comma-separated, e.g. Mon,Wed) or blank to finish: ").strip()
        if days_raw == "":
            break

        days = [d.strip().capitalize() for d in days_raw.split(",") if d.strip()]
        start = input("  Start time (e.g. 12:00AM): ").strip()
        end = input("  End time (e.g. 9:00AM): ").strip()

        try:
            window = BlockedWindow(days=days, start=start, end=end)
            blocked.append(window)
            print(f"  Added block: {days} {start}-{end}")
        except ValueError:
            print("  Couldn't parse that time (expected format like '9:00AM') -- skipping this block.")

    return blocked


def ask_career() -> set[str] | None:
    """Asks whether to restrict electives to undergrad, grad, or no filter."""
    raw = input(
        "Restrict electives to which career? [UG default / GR / ALL]: "
    ).strip().upper()
    if raw in ("", "UG"):
        return {"UG"}
    if raw == "ALL":
        return None
    return {raw}


# ---------- Fallback pool: fill remaining credits from cached courses ----------

def _min_credits_for_course(course: Course) -> float:
    """The cheapest valid way to take this course, credit-wise."""
    options = list(enroll_group_options(course))
    if not options:
        return 0.0
    return min(opt.credits for opt in options)


def load_all_cached_courses(
    roster: str,
    exclude_codes: set[str],
    careers: set[str] | None = DEFAULT_CAREERS,
) -> list[Course]:
    """
    Loads every course from every subject/category file already saved
    locally in cornell_data/ -- used as a fallback 'anything goes' pool
    when the user hasn't specified elective categories. This is limited to
    whatever's already been fetched in past runs, NOT the entire Cornell
    catalog (the API doesn't support an unfiltered "everything" query).

    careers filters the resulting pool by academic career (default:
    undergrad-only). Pass careers=None to include every career level.
    """
    courses = []
    seen_codes = set(exclude_codes)
    for path in Path("cornell_data").glob(f"{roster}_*.json"):
        for course in load_courses_from_file(path):
            if course.code not in seen_codes:
                courses.append(course)
                seen_codes.add(course.code)
    return filter_by_career(courses, careers)


def build_fallback_group_if_needed(
    required: list[Course],
    groups: list[RequirementGroup],
    roster: str,
    min_credits: float,
    careers: set[str] | None = DEFAULT_CAREERS,
) -> list[RequirementGroup]:
    """
    If no elective categories were given AND the required courses alone
    can't reach the credit minimum, auto-adds a fallback requirement group
    built from whatever courses are already cached locally.
    """
    if groups:
        return groups

    required_credits = sum(_min_credits_for_course(c) for c in required)
    if required_credits >= min_credits:
        return groups  # required courses alone already satisfy the minimum

    print(
        f"\nNo elective categories given, and required courses alone total "
        f"{required_credits} credits (below your {min_credits}-credit minimum)."
    )

    fallback_pool = load_all_cached_courses(roster, exclude_codes={c.code for c in required}, careers=careers)
    if not fallback_pool:
        print(
            "No other courses are cached yet to fill in automatically. "
            "Add a requirement category, or run fetch_cornell_courses.py for more subjects."
        )
        return groups

    credits_needed = max(0, min_credits - required_credits)
    num_filler = max(1, math.ceil(credits_needed / 4))  # assumes ~4 credits/course as a rough guess

    print(
        f"Filling in with up to {num_filler} course(s) from your "
        f"{len(fallback_pool)} previously-loaded course(s) to help reach your minimum. "
        f"For better/broader results, add a specific elective category instead."
    )

    return [RequirementGroup(name="Elective", pool=fallback_pool, num_needed=num_filler)]


# ---------- Demo ----------

if __name__ == "__main__":
    ROSTER = "FA26"

    required = ask_required_courses(roster=ROSTER)
    careers = ask_career()
    groups = ask_requirement_groups(roster=ROSTER, careers=careers)
    blocked = ask_blocked_windows()
    min_credits, max_credits = ask_credit_range(default_min=12, default_max=18)
    buffer_minutes = ask_buffer_minutes(default=15)

    groups = build_fallback_group_if_needed(required, groups, ROSTER, min_credits, careers=careers)

    results = plan_schedules_multi(
        required,
        groups,
        blocked,
        max_credits=max_credits,
        min_credits=min_credits,
        max_results=10,
        max_per_combo=1,
        buffer_minutes=buffer_minutes,
    )

    if results:
        print(f"\nFound {len(results)} valid schedule option(s):\n")
        for i, entry in enumerate(results, start=1):
            print(f"--- Option {i} ---")
            print_schedule(entry)