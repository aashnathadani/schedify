# Schedify -- Cornell schedule builder

## Where everything goes

`api.py` imports directly from `scheduler.py` and `plan_schedule.py` (plain
`from scheduler import ...`), and it serves the UI from a folder literally
named `frontend` sitting next to it. So the layout has to look like this in
VS Code:

```
schedify/                        <- open this folder in VS Code
├── scheduler.py                  REPLACE your existing file with this
├── plan_schedule.py               REPLACE your existing file with this
├── fetch_cornell_courses.py       unchanged -- leave as-is
├── fetch_by_requirement.py        unchanged
├── find_distribution_code.py      unchanged
├── check_fields.py                 unchanged
├── api.py                          NEW -- same folder as scheduler.py, not a subfolder
├── requirements.txt                NEW -- same folder
├── README.md                        NEW -- same folder (just docs)
└── frontend/                        NEW folder, must be named exactly "frontend"
    └── index.html                    NEW -- goes inside that folder
```

`cornell_data/` (the local JSON cache) will reappear automatically next to
everything else the first time you run it, same as before -- nothing to do
there.

## Running it

```bash
cd schedify
pip install -r requirements.txt
uvicorn api:app --reload
```

Then open **http://127.0.0.1:8000/**. Frontend and backend are the same
process, so that's the only command you need.

## What changed since the last pass

**1. Undergrad/grad filtering** (`scheduler.py`, `plan_schedule.py`)
- `Course` carries `acad_career` (e.g. `"UG"`, `"GR"`), read from the raw
  roster JSON's `enrollGroups[].acadCareer`.
- `filter_by_career(courses, careers={"UG"})` in `scheduler.py`; pass
  `careers=None` to disable filtering.
- Applied automatically inside `load_courses_by_distribution(...)` and
  `load_all_cached_courses(...)` -- the two places auto-built elective pools
  get assembled. Courses you name explicitly (`load_course`) are never
  filtered.
- Default is undergrad-only everywhere. I still haven't been able to hit
  Cornell's live API from this sandbox to confirm `acadCareer` is the exact
  field name on a real response -- worth a quick check on your first real
  run (inspect one course's raw JSON) since it's a one-line fix in
  `_build_course` if the name's slightly different.

**2. FastAPI wrapper** (`api.py`)
- `POST /api/schedules` runs the full pipeline and returns the top N
  schedules **ranked** by a compactness/days-used score (`_score_schedule`),
  not just the first N a random search happened to find.
- `GET /api/course/{subject}/{catalog_nbr}` and `GET /api/distribution/{code}`
  for single-course and requirement-code lookups/previews.
- `GET /api/subjects`, `GET /api/courses-in-subject/{subject}`, and
  `GET /api/distributions` -- power the frontend's autocomplete (see below).
- Diagnostic `print()` output from `plan_schedule.py` is captured and
  returned as a `warnings` array instead of vanishing into a server console.

**2. FastAPI wrapper** (`api.py`)
- `POST /api/schedules` runs the full pipeline and returns the top N
  schedules **ranked** by a compactness/days-used score (`_score_schedule`),
  not just the first N a random search happened to find.
- `GET /api/course/{subject}/{catalog_nbr}` and `GET /api/distribution/{code}`
  for single-course and requirement-code lookups/previews -- the latter is
  also used for live validation, see below.
- `GET /api/subjects`, `GET /api/courses-in-subject/{subject}`, and
  `GET /api/distributions` -- power the frontend's autocomplete.
- Diagnostic `print()` output from `plan_schedule.py` is captured and
  returned as a `warnings` array instead of vanishing into a server console.

**Category autocomplete -- how it actually works now:** I originally
guessed Cornell exposed a `/config/distributions.json` endpoint the same
way it exposes `/config/subjects.json`. It doesn't -- I checked Cornell's
real API documentation and confirmed the only `/config/` methods that
exist are `rosters`, `acadCareers`, `acadGroups`, `classLevels`, and
`subjects`. There's no endpoint that enumerates every distribution code,
full stop.

So `list_distribution_codes()` in `scheduler.py` now builds the list a
different way: it combines (1) a static seed list transcribed from
Cornell's public distribution-code documentation, and (2) codes pulled
directly out of whatever course data you've already cached locally (every
course's raw JSON carries its own codes under `crseAttrs` -- the same field
`find_distribution_code.py` already reads). That combined list grows the
more subjects you fetch, and it's what powers the dropdown suggestions as
you type.

On top of that, clicking **Add** for a requirement category now does a
**live check against Cornell's real search API** (`GET
/api/distribution/{code}`) before adding it -- if zero courses match, you
get an inline error immediately instead of silently adding a bad code.
Same live check was added for required courses via `GET
/api/course/{subject}/{catalog_nbr}`. Either way, even if you skip the
suggestions entirely and just type something and hit Add, invalid input
still can't sneak through unnoticed.

**3. Frontend** (`frontend/index.html`) -- **Schedify**
- Sidebar: term/roster, undergrad-vs-grad toggle, required courses,
  requirement categories, credit range, buffer minutes. Every field has a
  small hover-info "i" icon explaining what it's asking for.
- **Live autocomplete** on both the required-course field and the
  requirement-category field. Typing a course walks through two stages:
  first it suggests matching subject codes (fetched once and cached), then
  once a subject's recognized it fetches that subject's course list (also
  cached) and suggests matching catalog numbers with titles -- click one to
  add it. Even if you type something and hit Add without picking a
  suggestion, it's checked live against Cornell's real API before being
  added (see above) -- so only real courses/codes ever make it in, whether
  you use the dropdown or not.
- Main panel: a **when2meet-style blocked-time grid** -- every half-hour
  slot from 7am to midnight, Monday through Sunday, starts green (open).
  Click and drag to block times off; drag over red again to reopen it.
- Results: ranked tabs, each showing required courses (with titles) and
  electives (with which requirement category each one fulfills -- a
  no-category auto-fill now shows as simply "Elective" instead of the
  internal pool name), a color-coded weekly grid spanning all 7 days with
  hour labels, and a full section table. The page auto-scrolls to results
  once they're generated. Each schedule card has a hover-info icon next to
  its rank explaining the ranking method, and a **Download this schedule
  (PDF)** button that captures the actual rendered calendar (not just
  text) via `html2canvas` + `jsPDF`, loaded from cdnjs; there's also a
  **Download all options (PDF)** button above the tabs that builds a
  multi-page PDF, one schedule per page.

**4. More resilient error handling** (`api.py`)
- An unparseable/nonexistent required course or an invalid/empty
  requirement code was already skipped gracefully (with a warning) rather
  than failing the whole request -- that part was already working.
- What I added: if Cornell's live API itself errors out mid-lookup (a
  network hiccup, a bad response, etc.) rather than just returning "not
  found," that's now caught too and treated the same way -- skip that one
  item, log a warning, keep going -- instead of crashing the request with
  a raw 500. Same for malformed blocked-time windows and any unexpected
  failure inside the core schedule search, which now come back as a clean
  400/502 with a message instead of a stack trace.

I sanity-checked all three `.py` files compile and the frontend's embedded
JS parses cleanly, but couldn't do a real end-to-end run without network
access here -- do one real smoke test locally (add a required course, add a
requirement category like `CA-AG`, block off a few times, hit Generate) to
confirm real data comes back and everything renders as expected.

## Suggested next steps, roughly in order

1. **Smoke test locally**, fix any field-name mismatches (`acadCareer`
   being the main risk).
2. **Closed/waitlisted sections** -- filter these out if the roster JSON
   reports enrollment status, so people don't get handed schedules with no
   open seats.
3. **Real preference knobs** beyond the compactness heuristic -- e.g.
   "prefer specific professors," explicit "minimize days on campus" toggle.
4. Deploy: pin CORS down to your real frontend origin, decide whether
   `cornell_data/` ships pre-seeded or builds lazily per deployment.