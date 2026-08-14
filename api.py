"""
FastAPI wrapper around scheduler.py / plan_schedule.py.

Run locally with:
    uvicorn api:app --reload

Then open http://127.0.0.1:8000/ in a browser -- it serves the frontend/
folder directly, so there's nothing else to start.

Endpoints:
    GET  /api/course/{subject}/{catalog_nbr}   -- look up one course
    GET  /api/distribution/{code}              -- preview a requirement category
    POST /api/schedules                        -- generate ranked schedule options
"""

import contextlib
import io
from collections import defaultdict
from datetime import date, datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scheduler import (
    DEFAULT_CAREERS,
    load_course,
    load_courses_by_distribution,
    list_subjects,
    list_courses_in_subject,
    list_distribution_codes,
)
from plan_schedule import (
    BlockedWindow,
    RequirementGroup,
    build_fallback_group_if_needed,
    load_all_cached_courses,
    plan_schedules_multi,
)

app = FastAPI(title="Schedify API")

# Local dev: the frontend may be opened as a static file or served from a
# different port, so allow any origin. Tighten this before deploying.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class BlockedWindowIn(BaseModel):
    days: list[str] = Field(..., description="e.g. ['Mon', 'Wed', 'Fri']")
    start: str = Field(..., description="e.g. '12:00AM'")
    end: str = Field(..., description="e.g. '9:00AM'")


class RequirementGroupIn(BaseModel):
    code: str = Field(..., description="Distribution/requirement code, e.g. 'CA-AG'")
    num_needed: int = 1


class ScheduleRequest(BaseModel):
    roster: str = "FA26"
    required: list[str] = Field(default_factory=list, description="e.g. ['CS 2110', 'MATH 2940']")
    requirement_groups: list[RequirementGroupIn] = Field(default_factory=list)
    blocked_windows: list[BlockedWindowIn] = Field(default_factory=list)
    min_credits: float = 12
    max_credits: float = 18
    buffer_minutes: int = 15
    careers: list[str] | None = Field(
        default_factory=lambda: sorted(DEFAULT_CAREERS),
        description="Career codes to allow in auto-built elective pools, e.g. ['UG']. Use null/empty for no filtering.",
    )
    max_results: int = 5
    use_fallback_pool: bool = True


class MeetingOut(BaseModel):
    days: list[str]
    start: str | None
    end: str | None


class SectionOut(BaseModel):
    course: str
    component: str
    section_id: str
    class_nbr: int
    instructor: str
    meetings: list[MeetingOut]


class CourseOptionOut(BaseModel):
    course_code: str
    credits: float
    sections: list[SectionOut]


class RequiredCourseOut(BaseModel):
    code: str
    title: str


class ElectiveOut(BaseModel):
    code: str
    title: str
    requirement: str  # name of the requirement group/category this fulfills


class ScheduleOut(BaseModel):
    required_courses: list[RequiredCourseOut]
    electives: list[ElectiveOut]
    total_credits: float
    score: float
    options: list[CourseOptionOut]


class ScheduleResponse(BaseModel):
    count: int
    total_candidates_considered: int
    schedules: list[ScheduleOut]
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Ranking: turn "first N found" into "N good ones"
# ---------------------------------------------------------------------------

def _score_schedule(schedule) -> float:
    """
    Lower is better. Rewards compact days (less dead time between classes)
    and fewer distinct days with class at all (more full days off),
    without being a hard filter -- every conflict-free schedule still
    shows up, just ordered by how "good" it looks on paper.
    """
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for opt in schedule:
        for sec in opt.sections:
            for m in sec.meetings:
                if m.start is None or m.end is None:
                    continue
                for d in m.days:
                    by_day[d].append((m.start, m.end))

    gap_minutes = 0.0
    today = date.today()
    for day, times in by_day.items():
        times.sort()
        for (s1, e1), (s2, e2) in zip(times, times[1:]):
            gap = (datetime.combine(today, s2) - datetime.combine(today, e1)).total_seconds() / 60
            if gap > 0:
                gap_minutes += gap

    days_used = len(by_day)
    return gap_minutes + days_used * 20


def _to_schedule_out(entry: dict) -> ScheduleOut:
    options_out = []
    for opt in entry["schedule"]:
        sections_out = []
        for sec in opt.sections:
            sections_out.append(
                SectionOut(
                    course=sec.course,
                    component=sec.component,
                    section_id=sec.section_id,
                    class_nbr=sec.class_nbr,
                    instructor=sec.instructor or "Staff",
                    meetings=[
                        MeetingOut(
                            days=sorted(m.days),
                            start=m.start.strftime("%I:%M %p").lstrip("0") if m.start else None,
                            end=m.end.strftime("%I:%M %p").lstrip("0") if m.end else None,
                        )
                        for m in sec.meetings
                    ],
                )
            )
        options_out.append(
            CourseOptionOut(course_code=opt.course_code, credits=opt.credits, sections=sections_out)
        )

    return ScheduleOut(
        required_courses=[
            RequiredCourseOut(code=c["code"], title=c["title"]) for c in entry["required_chosen"]
        ],
        electives=[
            ElectiveOut(code=e["code"], title=e["title"], requirement=e["requirement"])
            for e in entry["electives_chosen"]
        ],
        total_credits=entry["total_credits"],
        score=_score_schedule(entry["schedule"]),
        options=options_out,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/subjects")
def get_subjects(roster: str = "FA26"):
    """Every subject code Cornell offers this roster -- powers the first
    stage of the required-course autocomplete (typing 'CS', 'MAT', etc)."""
    try:
        return {"subjects": list_subjects(roster)}
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach Cornell's API: {e}")


@app.get("/api/courses-in-subject/{subject}")
def get_courses_in_subject(subject: str, roster: str = "FA26"):
    """Every course in one subject -- powers the second stage of the
    required-course autocomplete, once a subject code looks complete."""
    try:
        return {"subject": subject.upper(), "courses": list_courses_in_subject(subject, roster)}
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach Cornell's API: {e}")


@app.get("/api/distributions")
def get_distributions(roster: str = "FA26"):
    """
    Best-effort list of distribution/requirement codes for autocomplete.
    Cornell's API has no endpoint that enumerates every valid code (see
    scheduler.list_distribution_codes), so this is a static seed list
    merged with codes seen in already-cached course data -- it grows as
    more subjects get fetched. Any code, in this list or not, is still
    validated for real against the live API when you generate schedules,
    or instantly via GET /api/distribution/{code}.
    """
    dists = list_distribution_codes(roster)
    return {
        "available": True,
        "distributions": dists,
        "note": (
            f"Any code you type is checked for real when "
            "you hit Add, whether or not it shows up as a suggestion here."
        ),
    }


@app.get("/api/course/{subject}/{catalog_nbr}")
def get_course(subject: str, catalog_nbr: str, roster: str = "FA26"):
    """Looks up one course by subject + catalog number, e.g. /api/course/CS/2110."""
    try:
        course = load_course(subject, catalog_nbr, roster=roster)
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach Cornell's API: {e}")

    if course is None:
        raise HTTPException(404, f"{subject.upper()} {catalog_nbr} not found in {roster}")

    return {
        "code": course.code,
        "title": course.title,
        "acad_career": course.acad_career or "unknown",
        "credit_options": sorted({eg.units_max or eg.units_min for eg in course.enroll_groups}),
    }


@app.get("/api/distribution/{code}")
def get_distribution(code: str, roster: str = "FA26", careers: str = "UG"):
    """Previews a distribution/requirement code, e.g. /api/distribution/CA-AG."""
    career_set = set(c.strip().upper() for c in careers.split(",") if c.strip()) or None

    try:
        pool = load_courses_by_distribution(code.upper(), roster=roster, careers=career_set)
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach Cornell's API: {e}")

    return {
        "code": code.upper(),
        "count": len(pool),
        "sample": [f"{c.code} -- {c.title}" for c in pool[:20]],
    }


@app.post("/api/schedules", response_model=ScheduleResponse)
def get_schedules(req: ScheduleRequest):
    careers = set(c.upper() for c in req.careers) if req.careers else None
    buf = io.StringIO()

    with contextlib.redirect_stdout(buf):
        required = []
        for entry in req.required:
            parts = entry.strip().split()
            if len(parts) < 2:
                print(f"Couldn't parse required course '{entry}' (expected e.g. 'CS 2110') -- skipping.")
                continue
            subject, catalog_nbr = parts[0].upper(), parts[1]
            try:
                course = load_course(subject, catalog_nbr, roster=req.roster)
            except Exception as e:
                print(f"Error looking up required course {subject} {catalog_nbr}: {e} -- skipping.")
                continue
            if course is None:
                print(f"Couldn't find required course {subject} {catalog_nbr} in {req.roster} -- skipping.")
                continue
            required.append(course)

        groups = []
        for rg in req.requirement_groups:
            try:
                pool = load_courses_by_distribution(rg.code.upper(), roster=req.roster, careers=careers)
            except Exception as e:
                print(f"Error looking up requirement code '{rg.code}': {e} -- skipping.")
                continue
            if not pool:
                print(f"No courses found for requirement code '{rg.code}' -- skipping.")
                continue
            groups.append(RequirementGroup(name=rg.code.upper(), pool=pool, num_needed=rg.num_needed))

        try:
            blocked = [
                BlockedWindow(days=bw.days, start=bw.start, end=bw.end) for bw in req.blocked_windows
            ]
        except ValueError as e:
            raise HTTPException(400, f"Couldn't parse a blocked time window: {e}")

        if req.use_fallback_pool:
            groups = build_fallback_group_if_needed(
                required, groups, req.roster, req.min_credits, careers=careers
            )

        try:
            results = plan_schedules_multi(
                required,
                groups,
                blocked,
                max_credits=req.max_credits,
                min_credits=req.min_credits,
                max_results=max(req.max_results * 8, 40),
                max_per_combo=2,
                buffer_minutes=req.buffer_minutes,
            )
        except Exception as e:
            raise HTTPException(502, f"Something went wrong while building schedules: {e}")

    ranked = sorted(results, key=lambda e: _score_schedule(e["schedule"]))

    # De-dupe schedules that landed on the exact same set of sections
    # (possible since sampling is random) before taking the top N.
    seen_signatures = set()
    deduped = []
    for entry in ranked:
        signature = tuple(sorted(sec.class_nbr for opt in entry["schedule"] for sec in opt.sections))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        deduped.append(entry)

    top = deduped[: req.max_results]
    warnings = [line for line in buf.getvalue().splitlines() if line.strip()]

    return ScheduleResponse(
        count=len(top),
        total_candidates_considered=len(results),
        schedules=[_to_schedule_out(e) for e in top],
        warnings=warnings,
    )


# Serve the frontend as static files at "/". Registered last so it acts as
# a catch-all and doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")