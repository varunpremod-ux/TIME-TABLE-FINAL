#!/usr/bin/env python3
"""Turn timetable PDFs / CSVs into timetable.json.

Usage: python parse_timetable.py out/timetable.json <pdf-or-folder> [more ...]

Give it one or more PDFs/CSVs, or a folder (every .pdf and .csv in it is read) and the
results are merged into one timetable.

Expects a table where each row is one class, with a header row containing
columns like: Day | Time | Class | Subject | Faculty | Roll No / Batch | Location.
Column order doesn't matter; header wording is matched loosely
(e.g. "Venue", "Room" and "Location" all map to location; "Professor",
"Teacher" and "Faculty" all map to faculty).
If a file has no Subject column, the file name is used as the subject
(Anatomy.pdf -> "Anatomy"). Blank cells in merged rows are filled from the row above.

Terms: a whole-department timetable has one section per term ("I Term", "VII Term" ...).
Set the environment variable TIMETABLE_TERM (e.g. VII, 7 or 7th) and only the classes
under that term's heading are kept. Headings can be text above a table or a merged row
inside it. A "Term" column also works.

Names: a file called names.csv (columns: Roll, Name) lets the site greet each student
("Welcome, Anil Bose") once they type their roll number. TIMETABLE_NAMES=full (default)
shows the name as written, first shows the first name only, off leaves names out.

Clinics: a file called clinics.csv (Batch, Rolls, From, To, Posting) says only WHICH posting
(subject) each batch of roll numbers has, and between which dates. Everything else (days,
class name, location, faculty) is taken from the timetable's own clinic rows, the rows
whose Class or Subject says "clinic", plus any department row inside the clinic hours (an internal
assessment, say), which belongs to the batch posted to that department. Clinics are shown ONCE a day, always at 10:30 - 13:00 (set TIMETABLE_CLINIC_TIME to
change it), with the details of all the timetable rows that apply merged into that one entry.
Each posting is placed on the days those rows apply. Optional Days,
Time and Location columns in clinics.csv override the timetable. Entries carry from/to
dates and the site shows them only on those dates.

Batches: when the Roll No column holds batch letters (A, B, C, D) instead of numbers,
they're converted to roll-number ranges using the class start time, because the batch
split differs by time of day. The split is set in DEFAULT_BATCH_RULES below, and can be
changed without touching code by adding a batches.json file (see README).

Holidays: a day marked "Holiday" (or "No classes") in the timetable, or a day that is
listed with nothing written under it, is saved as a holiday and shown as one on the site.

One week at a time: the site shows ONE week only, the newest week that has a weekly timetable
uploaded (the file rows' dates, a heading date range, or a date in the file name). Nothing else can
be browsed. When next week's timetable is uploaded, the site switches to that week and last
week disappears. TIMETABLE_WEEK=2026-09-21 in deploy.yml can force a particular week.
The overall block timetable, the clinics rotation and holidays are shown inside that week.

Weeks: every timetable file is treated as belonging to ONE week. The week comes from the dates
in the file's rows, or from a date range in a PDF's heading ("21 SEP 2026 - 27 SEP 2026"), or from
a date in the file name (surgery_2026-09-21.csv). Rows of that file then show only in that week.
A file with none of these repeats every week, and the Actions log warns about it.

Block timetables: an overall timetable that repeats every week for a whole block ("WEF 07 Sep 2026
to 24 Jan 2027") is tied to that block by a date range: two dates in the file name
(overall_2026-09-07_to_2027-01-24.csv), a date range in the PDF heading, or a Date value such
as "2026-09-07 to 2027-01-24". Its rows then repeat weekly inside that range only.

Week of the month: a Nth column ("1st, 2nd & 3rd") limits a weekly class to the 1st, 2nd, 3rd...
occurrence of its weekday in the month. This is how college sheets write "Dermatology - 1st, 2nd &
3rd day of the month" in a weekly slot. (Header: Nth, or "Week of month".)

Classes on certain dates: a Date (or Dates) value such as "1st, 2nd and 3rd", "1-3 Oct" or
"2026-10-01; 2026-10-02" makes a class show only on those days of the month or those exact dates.

Dates: a weekly programme with dates ("Monday, 21 Sep 2026") can be given a Date column
(YYYY-MM-DD), or a day cell / day heading that contains the date. Such rows apply only on
that date, so last week's programme doesn't repeat next week. Rows without a date repeat weekly.

Faculty codes: if the Faculty column holds short codes (AB, AKS) and the PDF lists
the full names somewhere (usually at the end: "AB - Dr. Anil Bose"), the codes are
replaced by the full names, using the key from the same file first, then faculty.csv,
then keys found in your other files. A file called faculty.csv (columns: Code,Name)
can add names that aren't listed in any timetable file.
"""
import calendar
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import pdfplumber

DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
# Whole weekday words only, so a legend row like "Satish" or "Monika" isn't read as a day.
DAY_WORD = (r"(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|"
            r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)")
DAY_ONLY = re.compile(rf"^{DAY_WORD}\.?$", re.I)
DAY_PREFIX = re.compile(rf"^{DAY_WORD}\b", re.I)

# Checked in this order, so "Class Room" is location and "Class Teacher" is faculty.
KEYWORDS = [
    ("term", ["term", "semester", "sem"]),
    ("faculty", ["faculty", "professor", "prof", "teacher", "lecturer", "staff", "instructor", "doctor", "incharge"]),
    ("location", ["location", "room", "venue", "hall", "place", "where"]),
    ("rolls", ["roll", "batch", "group", "division", "section"]),
    ("time", ["time", "slot", "timing", "period", "hour"]),
    ("day", ["day"]),
    ("date", ["date"]),
    ("subject", ["subject", "course", "topic", "module", "paper"]),
    ("class", ["class", "type", "session", "category"]),
]
CARRY = ("day", "time", "class", "subject", "faculty", "location")
TITLE = r"(?:dr|prof|mr|mrs|ms)"

# ---------- terms ----------
ROMAN = r"(?:XII|XI|X|IX|VIII|VII|VI|IV|V|III|II|I)"
ROMAN_OF = {i + 1: r for i, r in enumerate(["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"])}
TERM_TOKEN = rf"(?<![A-Za-z0-9])(?:({ROMAN})|(\d{{1,2}})(?:st|nd|rd|th)?)(?![A-Za-z0-9])"
TERM_HEADING = re.compile(
    rf"(?<![A-Za-z0-9])(?:{ROMAN}|\d{{1,2}}(?:st|nd|rd|th)?)\s*[-–]?\s*(?:term|semester|sem)\b"
    rf"|\b(?:term|semester|sem)\.?\s*[-–:]?\s*(?:{ROMAN}|\d{{1,2}})(?![A-Za-z0-9])", re.I)


def norm_term(s):
    """'VII Term' / '7th term' / '7' -> 'VII'"""
    m = re.search(TERM_TOKEN, s or "", re.I)
    if not m:
        return ""
    if m.group(1):
        return m.group(1).upper()
    return ROMAN_OF.get(int(m.group(2)), "")


def term_of_line(line):
    """A short heading line such as 'VII Term' -> 'VII', otherwise ''."""
    line = clean(line)
    if len(line) > 50:
        return ""
    m = TERM_HEADING.search(line)
    return norm_term(m.group(0)) if m else ""


def clean(cell):
    return re.sub(r"\s+", " ", cell or "").strip()


def classify(cell):
    if re.search(r"week\s*of\s*(the\s*)?month|wk\s*of\s*(the\s*)?month|^\s*nth\s*$|occurrence", cell, re.I):
        return "nth"
    tokens = re.findall(r"[a-z]+", cell.lower())
    for field, words in KEYWORDS:
        if any(t.startswith(w) for t in tokens for w in words):
            return field
    return None


def header_map(row):
    mapping = {}
    for i, cell in enumerate(row):
        field = classify(cell) if cell else None
        if field and field not in mapping.values():
            mapping[i] = field
    return mapping if len(set(mapping.values())) >= 3 else None


def norm_day(s):
    m = DAY_PREFIX.match(s.strip())
    return DAY_NAMES[m.group(1).lower()[:3]] if m else None


def norm_time(s):
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\bto\b", "-", s, flags=re.I)
    s = re.sub(r"\s*\bhrs?\b\.?", "", s, flags=re.I)
    s = re.sub(r"(?<=\d)\s*h\b\.?", "", s, flags=re.I)  # 1030h -> 1030
    s = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", s)
    s = re.sub(r"(?<![\d:.])([01]?\d|2[0-3])([0-5]\d)(?![\d:.])", r"\1:\2", s)  # 1030 -> 10:30
    return re.sub(r"\s*-\s*", " - ", s).strip()


def norm_rolls(s):
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"(\d+)\s*(?:\+|onwards?|and\s+above|&\s*above)", r"\1+", s, flags=re.I)  # "115 onwards" -> "115+"
    return re.sub(r"\bto\b", "-", s, flags=re.I).strip()


# ---------- batches -> roll ranges ----------
# Classes starting from `start_from` until `start_before` (24-hour HH:MM) use that split.
DEFAULT_BATCH_RULES = [
    {"start_from": "10:00", "start_before": "14:00",
     "batches": {"A": "1-38", "B": "39-76", "C": "77-114", "D": "115+"}},
    {"start_from": "14:00", "start_before": "24:00",
     "batches": {"A": "1-50", "B": "51-100", "C": "101-151"}},
]
OPEN_END = 10 ** 6
BATCH_TOKEN = re.compile(r"^(?:(?:batch|group|grp)\s*[-:]?\s*)?([A-Za-z])(?:\s*(?:batch|group))?$", re.I)


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def start_minutes(time):
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m?)?", time or "", re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()[:1]
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    if not ap and h < 7:
        h += 12
    return h * 60 + mi


def merge_ranges(specs):
    spans = []
    for spec in specs:
        for a, b, open_from, single in re.findall(r"(\d+)\s*-\s*(\d+)|(\d+)\s*\+|(\d+)", spec):
            if a:
                spans.append((int(a), int(b)))
            elif open_from:
                spans.append((int(open_from), OPEN_END))
            else:
                spans.append((int(single), int(single)))
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return ", ".join(f"{a}+" if b >= OPEN_END else str(a) if a == b else f"{a}-{b}" for a, b in merged)


def map_batches(rolls, time, rules):
    """('B', '14:00 - 16:00') -> ('51-100', 'B'). Returns (rolls, batch, problem)."""
    if re.search(r"\d", rolls) or re.search(r"\ball\b", rolls, re.I) or not rolls:
        return rolls, "", ""
    letters = []
    for tok in re.split(r"\s*(?:,|&|/|\+|;|\band\b)\s*", rolls):
        tok = tok.strip()
        if not tok:
            continue
        m = BATCH_TOKEN.match(tok)
        if not m:
            return rolls, "", ""  # not batch letters at all
        letters.append(m.group(1).upper())
    if not letters:
        return rolls, "", ""
    start = start_minutes(time)
    rule = next((r for r in rules if start is not None and hhmm(r["start_from"]) <= start < hhmm(r["start_before"])), None)
    if not rule:
        return rolls, "", f"batch {', '.join(letters)} at {time or 'no time'} matches no time window"
    table = {k.upper(): v for k, v in rule["batches"].items()}
    missing = [l for l in letters if l not in table]
    if missing:
        return rolls, "", f"batch {', '.join(missing)} is not defined for classes at {time}"
    return merge_ranges([table[l] for l in letters]), ", ".join(letters), ""


def load_batch_rules(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    return data["rules"] if isinstance(data, dict) else data


# ---------- holidays ----------
HOLIDAY_RE = re.compile(r"\b(holiday|no\s+class(?:es)?|no\s+lectures?|weekly\s+off|day\s+off|vacation)\b", re.I)
DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def holiday_note(cell):
    """'Holiday' -> '', 'Holiday (Diwali)' -> 'Diwali', 'No classes' -> ''"""
    if re.search(r"vacation", cell, re.I):
        return cell.strip()
    if not re.search(r"holiday", cell, re.I):
        return ""
    cell = re.sub(DAY_WORD, "", cell, count=1, flags=re.I) if DAY_PREFIX.match(cell) else cell
    return re.sub(r"holiday", "", cell, flags=re.I).strip(" -–—:()[]/,.")


# ---------- faculty codes -> full names ----------

def fkey(s):
    """'Dr. A.B.' / 'AB' / 'dr ab' -> 'AB'"""
    s = re.sub(rf"^{TITLE}\b\.?\s*", "", s.strip(), flags=re.I)
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def looks_code(s):
    bare = re.sub(rf"^{TITLE}\b\.?\s*", "", s.strip(), flags=re.I)
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9.]{0,5}", bare)) and bare.upper() == bare


def looks_name(s):
    s = s.strip()
    if re.match(rf"^{TITLE}\b", s, re.I):
        return len(s) > 4
    words = re.findall(r"[A-Za-z]{2,}", s)
    return len(words) >= 2 and all(w[0].isupper() for w in words[:2])


def add_pair(legend, code, name):
    code, name = clean(code), clean(name)
    if code and name and looks_code(code) and looks_name(name):
        legend.setdefault(fkey(code), name)


def legend_from_cells(row, legend):
    """Any neighbouring (code, name) cells in a row that isn't a class row."""
    cells = [c for c in row if c]
    for a, b in zip(cells, cells[1:]):
        add_pair(legend, a, b)


LEGEND_LINE = re.compile(r"^\s*(?:\d+[.)]?\s+)?([A-Za-z][A-Za-z.]{0,5})\s*[-–—:=]\s*(.{4,})$")


def legend_from_text(path, legend):
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                m = LEGEND_LINE.match(line)
                if m:
                    add_pair(legend, m.group(1), m.group(2))


def load_legend_csv(path):
    legend = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and fkey(row[0]) not in ("CODE", ""):
                if clean(row[0]) and clean(row[1]):
                    legend[fkey(row[0])] = clean(row[1])
    return legend


def resolve_faculty(raw, legend, missing):
    out = []
    for tok in re.split(r"\s*(?:,|/|&|\+|;|\band\b)\s*", raw):
        tok = tok.strip()
        if not tok:
            continue
        name = legend.get(fkey(tok))
        if name:
            out.append(name)
        else:
            if looks_code(tok):
                missing.add(tok)
            out.append(tok)
    return ", ".join(out)


# ---------- reading files ----------

def page_tables(page):
    tables = page.extract_tables()
    if not any(tables):
        tables = page.extract_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
    return tables


def subject_from_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"[_\-]+", " ", stem).strip()


def pdf_rows(path):
    """Yield table rows, and ("TERM", "VII") markers when a term heading is passed."""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.find_tables()
            if not tables:
                tables = page.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
            events = [(tb.bbox[1], 1, tb) for tb in tables]
            try:
                lines = page.extract_text_lines()
            except Exception:
                lines = []
            for ln in lines:
                term = term_of_line(ln["text"])
                inside = any(tb.bbox[1] - 1 <= ln["top"] <= tb.bbox[3] for tb in tables)
                if term and not inside:
                    events.append((ln["top"], 0, ("TERM", term)))
            for _, kind, payload in sorted(events, key=lambda e: (e[0], e[1])):
                if kind == 0:
                    yield payload
                else:
                    yield from payload.extract()


def csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.reader(f)


def parse(path):
    default_subject = subject_from_name(path)
    is_csv = path.lower().endswith(".csv")
    rows = csv_rows(path) if is_csv else pdf_rows(path)
    entries, colmap, carry, legend = [], None, {}, {}
    holidays, days_seen = [], set()  # days_seen holds (term, weekday, iso date or '')
    term = ""
    for raw in rows:
        if isinstance(raw, tuple):
            term, carry = raw[1], {}
            continue
        row = [clean(c) for c in raw]
        if not any(row):
            continue
        only = [c for c in row if c]
        if len(only) == 1 and term_of_line(only[0]):
            term, carry = term_of_line(only[0]), {}
            continue
        hm = header_map(row)
        if hm:
            colmap = hm
            continue
        if not colmap:
            legend_from_cells(row, legend)
            continue
        filled = [c for c in row if c]
        day_idx = next((i for i, f in colmap.items() if f == "day"), None)

        # "Holiday" written under a day: the row has nothing but the day and the holiday text.
        hol = next((c for c in filled if len(c) <= 40 and HOLIDAY_RE.search(c)), None)
        others = [c for i, c in enumerate(row)
                  if c and c is not hol and colmap.get(i) not in ("day", "term", "rolls", "date", "nth")
                  and not DAY_ONLY.match(c) and not term_of_line(c)]
        if hol and not others:
            term_idx = next((i for i, f in colmap.items() if f == "term"), None)
            row_term = norm_term(row[term_idx]) if term_idx is not None and term_idx < len(row) else ""
            date_idx = next((i for i, f in colmap.items() if f == "date"), None)
            hdate, hspan = "", None
            if date_idx is not None and date_idx < len(row):
                hspec = parse_dates_spec(row[date_idx])
                hdate = (hspec.get("on") or [""])[0]
                hspan = hspec.get("range")
            hdate = hdate or date_in_text(hol) or carry.get("date", "")
            day = ((norm_day(hol) if DAY_PREFIX.match(hol) else None)
                   or next((norm_day(c) for c in filled if DAY_ONLY.match(c)), None)
                   or (norm_day(row[day_idx]) if day_idx is not None and day_idx < len(row) and row[day_idx] else None)
                   or carry.get("day"))
            if hspan:  # e.g. term-end vacation: every day in the range is a holiday
                d0, d1 = date.fromisoformat(hspan[0]), date.fromisoformat(hspan[1])
                for i in range((d1 - d0).days + 1):
                    dd = d0 + timedelta(days=i)
                    holidays.append({"term": row_term or term, "day": DAY_ORDER[dd.weekday()],
                                     "note": holiday_note(hol), "date": dd.isoformat(), "explicit": True})
                continue
            if hdate:
                day = DAY_ORDER[datetime.strptime(hdate, "%Y-%m-%d").weekday()]
            if day:
                holidays.append({"term": row_term or term, "day": day, "note": holiday_note(hol),
                                 "date": hdate, "explicit": True})
                continue

        heading = day_heading(filled[0]) if len(filled) == 1 else None
        if heading:
            carry = {"day": heading[0]}
            if heading[1]:
                carry["date"] = heading[1]
            days_seen.add((term, heading[0], heading[1]))  # a day with nothing under it turns out to be a holiday
            continue

        # A row whose day cell isn't a weekday is not a class (e.g. a faculty list row).
        if day_idx is not None and day_idx < len(row) and row[day_idx] and not norm_day(row[day_idx]):
            legend_from_cells(row, legend)
            continue

        rec = {}
        for idx, field in colmap.items():
            val = row[idx] if idx < len(row) else ""
            if field == "day" and val:
                day_date = date_in_text(val)
                val = norm_day(val) or ""
                if val and val != carry.get("day"):
                    carry = {"day": val}
                if day_date:
                    carry["date"] = day_date
            elif field == "date":
                pass  # kept as written; read by parse_dates_spec below
            elif field == "time" and val:
                val = norm_time(val)
                if val != carry.get("time"):
                    for k in ("class", "subject", "faculty", "location"):
                        carry.pop(k, None)
            elif field == "rolls":
                val = norm_rolls(val)
            elif field == "term":
                val = norm_term(val)
            rec[field] = val

        own_day = rec.get("day", "")
        for field in CARRY:
            if field in colmap.values():
                if rec.get(field):
                    carry[field] = rec[field]
                else:
                    rec[field] = carry.get(field, "")
        rec["subject"] = rec.get("subject") or default_subject
        raw_date = rec.get("date", "")
        spec = parse_dates_spec(raw_date or carry.get("date", ""))
        if raw_date and len(spec.get("on", [])) == 1:
            carry["date"] = spec["on"][0]  # only a single full date is ever carried to blank cells below
        on = spec.get("on", [])
        if len(on) == 1:  # one exact date: the date decides the weekday
            rec["day"] = DAY_ORDER[datetime.strptime(on[0], "%Y-%m-%d").weekday()]
        elif not own_day and (spec.get("dom") or len(on) > 1 or spec.get("range")):
            rec["day"] = "*"  # a class on certain dates, with no weekday given, runs on any weekday
        elif not rec.get("day"):
            rec["day"] = carry.get("day", "")
        if not rec.get("day"):
            continue
        entry = {
            "term": rec.get("term") or term,
            "day": rec["day"],
            "time": rec.get("time", ""),
            "class": rec.get("class", ""),
            "subject": rec["subject"],
            "faculty": rec.get("faculty", ""),
            "rolls": rec.get("rolls") or "All",
            "batch": "",
            "location": rec.get("location", ""),
        }
        if on:
            entry["from"], entry["to"] = on[0], on[-1]
            if len(on) > 1:
                entry["on"] = on
        elif spec.get("range"):
            entry["from"], entry["to"] = spec["range"]
        nth = parse_nth(rec.get("nth", ""))
        if nth:
            entry["nth"] = nth
        if spec.get("dom"):
            entry["dom"] = spec["dom"]
            if spec.get("mon"):
                entry["mon"] = spec["mon"]
        entries.append(entry)

    if not is_csv:
        legend_from_text(path, legend)
    first_text = ""
    if not is_csv:
        legend_from_text(path, legend)
        with pdfplumber.open(path) as pdf:
            first_text = (pdf.pages[0].extract_text() or "")[:3000] if pdf.pages else ""

    # Which week is this file for?
    week = None
    dated = [e for e in entries if e.get("from")]
    if dated:
        lo, hi = min(e["from"] for e in dated), max(e["to"] for e in dated)
        if (date.fromisoformat(hi) - date.fromisoformat(lo)).days <= 6:
            week = week_of(lo)
    week = week or week_from_text(first_text) or week_from_name(path)
    if week:
        for e in entries:
            if not e.get("from"):
                e["from"], e["to"] = week
    # Days that were listed with nothing under them are holidays.
    for t, d, dt in sorted(days_seen):
        if dt:
            busy = any(e["term"] == t and e.get("from") == dt for e in entries)
        else:
            busy = any(e["term"] == t and e["day"] == d for e in entries)
        if not busy:
            holidays.append({"term": t, "day": d, "note": "", "date": dt, "explicit": False})
    if week:
        for h in holidays:
            if not h.get("date"):
                h["date"] = (date.fromisoformat(week[0]) + timedelta(days=DAY_ORDER.index(h["day"]))).isoformat()
    return entries, legend, holidays, week


# ---------- main ----------

# ---------- clinics.csv ----------
CLINICS_FILE = "clinics.csv"
DEFAULT_CLINIC_TIME = "10:30 - 13:00"
DEFAULT_CLINIC_DAYS = DAY_ORDER[:6]
CLINIC_HEADERS = {
    "location": ["location", "venue", "place", "room", "hospital"],
    "time": ["time", "timing"],
    "days": ["days", "day"],
    "from": ["from", "start", "begin"],
    "to": ["to", "end", "until", "till"],
    "rolls": ["roll", "rolls"],
    "batch": ["batch", "group"],
    "posting": ["posting", "subject", "department", "dept", "clinic", "clinics", "unit"],
}
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d %m %Y"]


def clinic_field(cell):
    tokens = re.findall(r"[a-z]+", cell.lower())
    for field, words in CLINIC_HEADERS.items():
        if any(t.startswith(w) for t in tokens for w in words):
            return field
    return None


def parse_date(s):
    s = re.sub(r"\bsept\b", "Sep", clean(s), flags=re.I)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


DATE_TEXT = re.compile(r"(?<!\d)(\d{1,2})[\s/.-]+([A-Za-z]{3,9}|\d{1,2})[\s/.,-]+(\d{4})(?!\d)")


def date_in_text(s):
    """'Monday, 21 Sep 2026' -> '2026-09-21' ('' if there is no date)"""
    m = DATE_TEXT.search(s or "")
    return parse_date(" ".join(m.groups())) if m else ""


def parse_date_any(s):
    return parse_date(s) or date_in_text(s)


def day_heading(cell):
    """A cell that is only a weekday, optionally with a date: ('Monday', '2026-09-21') or None."""
    m = DAY_PREFIX.match(cell.strip())
    if not m:
        return None
    rest = cell.strip()[m.end():]
    iso = date_in_text(rest)
    rest = DATE_TEXT.sub("", rest)
    if re.sub(r"[\s.,:;()\-]+", "", rest):
        return None
    return norm_day(cell), iso


MONTHS = {m.lower()[:3]: i for i, m in enumerate(calendar.month_name) if m}


def monday_of(d):
    return d - timedelta(days=d.weekday())


def week_of(iso):
    """'2026-09-23' -> ('2026-09-21', '2026-09-27'), the Monday to Sunday containing it."""
    m = monday_of(date.fromisoformat(iso))
    return m.isoformat(), (m + timedelta(days=6)).isoformat()


def week_from_text(text):
    """A heading such as '21 SEP 2026 - 27 SEP 2026' -> ('2026-09-21', '2026-09-27')."""
    m = re.search(r"(\d{1,2})\s*([A-Za-z]{3,9})\.?,?\s*(\d{4})\s*(?:-|–|—|to)\s*(\d{1,2})\s*([A-Za-z]{3,9})\.?,?\s*(\d{2,4})(?!\d)",
                  text or "", re.I)
    if not m:
        return None
    y2 = m.group(6) if len(m.group(6)) == 4 else str(int(m.group(3)) // 100 * 100 + int(m.group(6)))
    a = parse_date(f"{m.group(1)} {m.group(2)} {m.group(3)}")
    b = parse_date(f"{m.group(4)} {m.group(5)} {y2}")
    if a and b and a <= b and (date.fromisoformat(b) - date.fromisoformat(a)).days <= 400:
        return a, b  # one week for a weekly programme, or a whole block for an overall timetable
    return None


def week_from_name(path):
    """A date in the file name: surgery_2026-09-21.csv -> that week (Monday to Sunday)."""
    name = os.path.splitext(os.path.basename(path))[0]
    isos = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", name)
    valid = [d for d in isos if parse_date(d)]
    if len(valid) >= 2 and valid[0] <= valid[1]:
        return valid[0], valid[1]  # a block: overall_2026-09-07_to_2027-01-24.csv
    if valid:
        return week_of(valid[0])
    m = re.search(r"(?<!\d)(\d{1,2})[-_ ]+(\d{1,2})[-_ ]+([A-Za-z]{3,9})[-_ ]+(\d{4})(?!\d)", name)
    if m:
        a = parse_date(f"{m.group(1)} {m.group(3)} {m.group(4)}")
        b = parse_date(f"{m.group(2)} {m.group(3)} {m.group(4)}")
        if a and b and a <= b:
            return a, b
    return None


def parse_nth(s):
    """'1st, 2nd & 3rd' -> [1, 2, 3]; '4th & 5th' -> [4, 5]; '1-4' -> [1, 2, 3, 4] (week of the month)"""
    s = clean(s).lower()
    if not s or s in ("all", "every", "each"):
        return []
    nums = set()
    rng = r"(\d)(?:st|nd|rd|th)?\s*(?:-|–|to)\s*(\d)(?:st|nd|rd|th)?"
    for m in re.finditer(rng, s):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= 5:
            nums |= set(range(a, b + 1))
    rest = re.sub(rng, " ", s)
    nums |= {int(x) for x in re.findall(r"(?<!\d)([1-5])(?:st|nd|rd|th)?(?!\d)", rest)}
    return sorted(nums)


def parse_dates_spec(s):
    """What dates does a Date / Dates value mean?
    '2026-09-21'              -> {'on': ['2026-09-21']}
    '1, 2 Oct 2026'-style     -> {'on': [...]} when the year is given
    '1st, 2nd and 3rd'        -> {'dom': [1, 2, 3]}          (those days of the month)
    '1-3 Oct'                 -> {'dom': [1, 2, 3], 'mon': [10]}
    """
    s = clean(s)
    if not s:
        return {}
    # "2026-09-07 to 2027-01-24" (or two full dates written out): a stretch of time, not two exact days
    span = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:to|until|till|-|–|—)\s*(\d{4}-\d{2}-\d{2})", s) or \
        re.search(r"(\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{4})\s*(?:to|until|till|-|–|—)\s*(\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{4})", s)
    if span:
        a, b = parse_date(span.group(1)), parse_date(span.group(2))
        if a and b and a <= b:
            return {"range": [a, b]}
    on = set(re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", s))
    rest = re.sub(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", " ", s)
    for m in re.finditer(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", rest):
        a = parse_date(f"{m.group(1)} {m.group(3)} {m.group(4)}")
        b = parse_date(f"{m.group(2)} {m.group(3)} {m.group(4)}")
        if a and b and a <= b:
            d0 = date.fromisoformat(a)
            on |= {(d0 + timedelta(days=i)).isoformat() for i in range((date.fromisoformat(b) - d0).days + 1)}
    rest = re.sub(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", " ", rest)
    # "1, 2 and 3 Oct 2026" or "21 Sep 2026": several days sharing one month and year
    listed = re.compile(r"((?:\d{1,2}(?:st|nd|rd|th)?[\s,&]*(?:and[\s,]*)?)+)([A-Za-z]{3,9})\.?,?\s+(\d{4})(?!\d)")
    for m in listed.finditer(rest):
        for n in re.findall(r"\d{1,2}", m.group(1)):
            d = parse_date(f"{n} {m.group(2)} {m.group(3)}")
            if d:
                on.add(d)
    rest = listed.sub(" ", rest)
    for m in DATE_TEXT.finditer(rest):
        d = parse_date(" ".join(m.groups()))
        if d:
            on.add(d)
    if on:
        return {"on": sorted(on)}
    mons = sorted({MONTHS[w[:3].lower()] for w in re.findall(r"[A-Za-z]{3,9}", rest) if w[:3].lower() in MONTHS})
    days = set()
    for m in re.finditer(r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|to)\s*(\d{1,2})(?:st|nd|rd|th)?", rest, re.I):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= 31:
            days |= set(range(a, b + 1))
    rest2 = re.sub(r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|to)\s*(\d{1,2})(?:st|nd|rd|th)?", " ", rest, flags=re.I)
    days |= {int(x) for x in re.findall(r"(?<![\d/:.-])(\d{1,2})(?:st|nd|rd|th)?(?![\d/:.-])", rest2) if 1 <= int(x) <= 31}
    if not days:
        return {}
    out = {"dom": sorted(days)}
    if mons:
        out["mon"] = mons
    return out


def parse_days(s):
    idx = {n[:3].lower(): i for i, n in enumerate(DAY_ORDER)}
    out = []
    for part in re.split(r"[,;/&]|\band\b", clean(s)):
        part = part.strip()
        m = re.fullmatch(r"([A-Za-z]+)\s*(?:-|–|to)\s*([A-Za-z]+)", part)
        if m and m.group(1)[:3].lower() in idx and m.group(2)[:3].lower() in idx:
            a, b = idx[m.group(1)[:3].lower()], idx[m.group(2)[:3].lower()]
            out += DAY_ORDER[a:b + 1]
        elif part[:3].lower() in idx:
            out.append(DAY_ORDER[idx[part[:3].lower()]])
    return list(dict.fromkeys(out))


def load_clinics(path):
    rows, problems, header = [], [], None
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, raw in enumerate(csv.reader(f), 1):
            row = [clean(c) for c in raw]
            if not any(row):
                continue
            if header is None:
                header = {i: clinic_field(c) for i, c in enumerate(row) if c and clinic_field(c)}
                need = {"from", "to", "posting"}
                if not need <= set(header.values()) or not {"rolls", "batch"} & set(header.values()):
                    sys.exit("clinics.csv needs a header row with columns like: "
                             "Batch, Rolls, From, To, Posting, Location")
                continue
            rec = {fld: (row[i] if i < len(row) else "") for i, fld in header.items()}
            start, end = parse_date(rec.get("from", "")), parse_date(rec.get("to", ""))
            if not start or not end or not rec.get("posting"):
                problems.append(f"line {n}: needs a posting and valid From / To dates (use YYYY-MM-DD)")
                continue
            rows.append({"batch": rec.get("batch", "").upper(), "rolls": norm_rolls(rec.get("rolls", "")),
                         "from": start, "to": end, "posting": rec["posting"],
                         "location": rec.get("location", ""), "days": parse_days(rec.get("days", "")),
                         "time": norm_time(rec["time"]) if rec.get("time") else ""})
    return rows, problems


# Words that name a department, so a timetable clinic row for "Paed" or "Obst & Gynae" can be
# matched to the same subject on the clinics sheet.
DEPT_WORDS = {
    "medicine": r"\bmedicine\b|\b(?:gen(?:eral)?|int(?:ernal)?)\.?\s*med\b",
    "surgery": r"\bsurg\w*",
    "obg": r"\bobg\b|\bobst\w*|\bgyn\w*|\bo\s*&\s*g\b",
    "paed": r"\bpa?ediatric\w*|\bpaed\w*|\bpeds\b",
    "ortho": r"\bortho\w*",
    "ent": r"\bent\b|\botorhino\w*|\botolaryng\w*",
    "ophthal": r"\bophthal\w*|\bopthal\w*",
    "psy": r"\bpsy\w*",
    "radio": r"\bradio\w*",
    "derm": r"\bderm\w*|\bdvl\b",
    "anaes": r"\banae?s\w*|\banesth\w*",
}


def dept_keys(text):
    return {k for k, pat in DEPT_WORDS.items() if re.search(pat, text or "", re.I)}


def applies_on(e, d):
    """Does timetable entry e run on date d (a datetime.date)?"""
    iso = d.isoformat()
    if e.get("from") and iso < e["from"]:
        return False
    if e.get("to") and iso > e["to"]:
        return False
    if e.get("on") and iso not in e["on"]:
        return False
    if e.get("dom") and d.day not in e["dom"]:
        return False
    if e.get("mon") and d.month not in e["mon"]:
        return False
    if e.get("nth") and (d.day - 1) // 7 + 1 not in e["nth"]:
        return False
    if e.get("skip") and iso in e["skip"]:
        return False
    return e["day"] in ("*", DAY_ORDER[d.weekday()])


def time_bounds(t):
    """'08:00 - 08:45' -> (480, 525); None when the time can't be read."""
    parts = [p.strip() for p in (t or "").split(" - ")]
    s = start_minutes(parts[0]) if parts and parts[0] else None
    if s is None:
        return None
    e = start_minutes(parts[1]) if len(parts) > 1 and parts[1] else None
    if e is None or e <= s:
        e = s + 60
    return s, e


def apply_overrides(entries):
    """Two rows can end up describing the same lecture: the overall block timetable's generic slot
    ("Theory - Surgery") next to a department's own row for it, or the very same class read from two
    different uploads with slightly different wording ("Paediatrics" vs "Pediatrics", "PE 20.7" vs
    "PE: 20.7"). Wherever two rows share a weekday, department and overlapping time, on the dates
    they share only one is kept: a generic block-timetable slot always loses to a specific row; between
    two specific rows, the more detailed one (longer class/faculty/location text) is kept, so the
    lecture is listed once."""
    FOREVER = 10 ** 6
    span = lambda e: ((date.fromisoformat(e["to"]) - date.fromisoformat(e["from"])).days + 1
                      if e.get("from") and e.get("to") else FOREVER)
    is_template = lambda e: 7 < span(e) < FOREVER  # a row from a multi-week block timetable
    same_subject = lambda a, b: re.sub(r"\W", "", a.lower()) == re.sub(r"\W", "", b.lower()) or \
        bool(dept_keys(a) & dept_keys(b))
    quality = lambda e: len(e.get("class", "")) + len(e.get("faculty", "")) + len(e.get("location", ""))
    live = [e for e in entries if not e.get("rot") and e["day"] != "*" and e.get("from") and e.get("to")]

    for i, a in enumerate(live):
        ab = time_bounds(a["time"])
        if not ab:
            continue
        for b in live[i + 1:]:
            if b["day"] != a["day"] or not same_subject(a["subject"], b["subject"]):
                continue
            if a["rolls"] not in ("All", "") and b["rolls"] not in ("All", "") and a["rolls"] != b["rolls"]:
                continue  # a row for some batches only doesn't override the slot for the others
            bb = time_bounds(b["time"])
            if not bb or not (ab[0] < bb[1] and bb[0] < ab[1]):
                continue
            lo = max(date.fromisoformat(a["from"]), date.fromisoformat(b["from"]))
            hi = min(date.fromisoformat(a["to"]), date.fromisoformat(b["to"]))
            if lo > hi:
                continue
            at, bt = is_template(a), is_template(b)
            if at != bt:
                loser = a if at else b  # a generic block-timetable slot always loses to a specific row
            else:
                loser = b if quality(a) >= quality(b) else a  # otherwise keep the more detailed row
            d = lo
            while d <= hi:
                if d.weekday() == DAY_ORDER.index(a["day"]) and applies_on(a, d) and applies_on(b, d):
                    loser.setdefault("skip", []).append(d.isoformat())
                d += timedelta(days=1)
    return entries


def add_clinics(entries, rows, rules, term):
    """Replace the timetable's own clinic rows with one entry per posting per date.

    The posting (subject) and roll numbers come from clinics.csv. Day, time, class, location and
    faculty come from the timetable's clinic rows that apply on that date, but only from rows that
    belong to the posting: rows that name the same department (in the Subject, Class, venue, or the
    file name), plus rows that name no department at all. A row that names another department
    (for example Paediatrics) is never used for Surgery. In a week where the posting has no clinic
    programme yet, it still shows at the usual clinic slot with no venue or faculty."""
    clinic_time = norm_time(os.environ.get("TIMETABLE_CLINIC_TIME", "")) or DEFAULT_CLINIC_TIME
    cb = time_bounds(clinic_time)

    def in_clinic_slot(e):  # the row sits inside the clinic hours (a little slack either side)
        tb = time_bounds(e["time"])
        return bool(tb and cb and tb[0] >= cb[0] - 30 and tb[1] <= cb[1] + 30 and tb[1] > cb[0] and tb[0] < cb[1])

    def names_department(e):
        return bool(dept_keys(f"{e['subject']} {e['class']}") or dept_keys(e["location"])
                    or dept_keys(os.path.splitext(e.get("_src", ""))[0]))

    # A clinic row, or any department row in the clinic hours (for example an internal assessment):
    # the students are in that department's clinics then, so it belongs to the batch posted there.
    is_clinic = lambda e: re.search(r"clinic", f"{e['class']} {e['subject']}", re.I) or (
        in_clinic_slot(e) and names_department(e))
    clin = [dict(e) for e in entries if is_clinic(e)]
    entries = [e for e in entries if not is_clinic(e)]
    for e in clin:  # subject/class first, then the venue ("Paed Ward"), then the file name
        e["_k"] = (dept_keys(f"{e['subject']} {e['class']}") or dept_keys(e["location"])
                   or dept_keys(os.path.splitext(e.get("_src", ""))[0]))

    def merge(rows_, blank=False):
        uniq = lambda f: [v for i, v in enumerate(x[f] for x in rows_) if v and v not in [y[f] for y in rows_[:i]]]
        cls = uniq("class")
        return (cls[0] if cls else "Clinics", "" if blank else " / ".join(uniq("location")),
                "" if blank else ", ".join(uniq("faculty")))

    out, problems, unmatched = [], set(), set()
    for r in rows:
        pk = dept_keys(r["posting"])
        belongs = lambda e: not e["_k"] or bool(pk & e["_k"])
        named_here = any(pk & e["_k"] for e in clin)
        if not named_here and any(e["_k"] for e in clin):
            unmatched.add(r["posting"])
        mine = [e for e in clin if belongs(e)]
        others = [e for e in clin if e["_k"] and not (pk & e["_k"])]
        end = date.fromisoformat(r["to"])
        d = date.fromisoformat(r["from"])
        while d <= end:
            today = sorted((e for e in mine if applies_on(e, d)),  # rows naming this subject, then rows at the clinic time, first
                           key=lambda e: (0 if (pk & e["_k"]) else 1, 0 if e["time"] == clinic_time else 1))
            pairs = []  # (time, class, location, faculty): at most one clinic entry per posting per day
            if r["days"] or r["time"]:
                if not r["days"] or DAY_ORDER[d.weekday()] in r["days"]:
                    pairs.append((r["time"] or clinic_time,) + (merge(today) if today else ("Clinics", "", "")))
            elif today:
                pairs.append((clinic_time,) + merge(today))
            elif d.weekday() <= 5:
                week = [monday_of(d) + timedelta(days=i) for i in range(7)]
                if not clin or not any(applies_on(e, w) for w in week for e in mine):
                    # No programme for this posting this week (or no clinic rows at all): show it at
                    # the usual clinic time with no venue or faculty, never another department's people.
                    pairs.append((clinic_time, "Clinics", "", ""))
            for t, cls, location, faculty in pairs:
                rolls, batch = r["rolls"], r["batch"]
                if not rolls and batch:
                    rolls, _, problem = map_batches(batch, t, rules)
                    if problem:
                        problems.add(problem)
                out.append({"term": term, "day": DAY_ORDER[d.weekday()], "time": t, "class": cls,
                            "subject": r["posting"], "faculty": faculty, "rolls": rolls or "All", "batch": batch,
                            "location": r["location"] or location, "from": d.isoformat(), "to": d.isoformat(),
                            "pfrom": r["from"], "pto": r["to"], "rot": 1})
            d += timedelta(days=1)
    if unmatched:
        problems.add("no clinic row in the timetable names " + ", ".join(sorted(unmatched))
                     + ", so those are shown without a venue or faculty")
    return entries + out, bool(clin), problems


# ---------- names.csv ----------
NAMES_FILE = "names.csv"


def tidy_name(s, style):
    s = clean(s)
    if not s:
        return ""
    if s.isupper() or s.islower():
        s = s.title()  # "ANIL KUMAR BOSE" -> "Anil Kumar Bose"
    if style == "first":
        parts = s.split()
        s = next((p for p in parts if len(p.strip(".")) > 1), parts[0])
    return s


def load_names(path, style):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [[clean(c) for c in r] for r in csv.reader(f)]
    rows = [r for r in rows if any(r)]
    if not rows:
        return {}, 0
    head = [c.lower() for c in rows[0]]
    col = lambda words: next((i for i, c in enumerate(head) if any(w in c for w in words)), None)
    ri, ni = col(["roll", "sr", "serial", "reg", "no"]), col(["name", "student"])
    if ri is None or ni is None or ri == ni:
        ri, ni, body = 0, 1, (rows if rows[0][0][:1].isdigit() else rows[1:])
    else:
        body = rows[1:]
    names, dupes = {}, 0
    for r in body:
        if len(r) <= max(ri, ni):
            continue
        m = re.search(r"\d+", r[ri])
        name = tidy_name(r[ni], style)
        if not m or not name:
            continue
        key = str(int(m.group(0)))
        dupes += key in names
        names[key] = name
    return names, dupes


SUPPORTED = (".pdf", ".csv")
IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".heic")
LEGEND_FILE = "faculty.csv"
BATCH_FILE = "batches.json"


def collect(args):
    files, legend_files, batch_files, clinic_files, name_files = [], [], [], [], []
    for a in args:
        if os.path.isdir(a):
            for f in sorted(os.listdir(a)):
                low, full = f.lower(), os.path.join(a, f)
                if low == LEGEND_FILE:
                    legend_files.append(full)
                elif low == BATCH_FILE:
                    batch_files.append(full)
                elif low == CLINICS_FILE:
                    clinic_files.append(full)
                elif low == NAMES_FILE:
                    name_files.append(full)
                elif low.endswith(SUPPORTED):
                    files.append(full)
                elif low.endswith(IMAGES):
                    print(f"{f}: IGNORED. Photos can't be read automatically. "
                          f"Turn it into a .csv (see README) and upload that instead.")
        elif os.path.exists(a):
            files.append(a)
        else:
            sys.exit(f"Can't find {a}")
    return files, legend_files, batch_files, clinic_files, name_files


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dst = sys.argv[1]
    files, legend_files, batch_files, clinic_files, name_files = collect(sys.argv[2:])
    rules = load_batch_rules(batch_files[0]) if batch_files else DEFAULT_BATCH_RULES
    print(f"Batch rules: {'batches.json' if batch_files else 'built-in defaults'}")
    if not files:
        sys.exit("No PDF or CSV files found. Upload your timetables to the repo's main page.")
    global_legend = {}
    for lf in legend_files:
        global_legend.update(load_legend_csv(lf))
        print(f"{os.path.basename(lf)}: {len(global_legend)} faculty names")

    target = norm_term(os.environ.get("TIMETABLE_TERM", ""))
    parsed = [(f, *parse(f)) for f in files]  # (file, entries, own faculty key, holidays, week)
    # Faculty keys can sit in a different file from the classes that use them.
    pooled = {}
    for _, _, lg, _, _ in parsed:
        for k, v in lg.items():
            pooled.setdefault(k, v)

    entries, seen, unresolved, batch_problems = [], set(), {}, {}
    weekly_files = []  # (monday, file name) for every file that is tied to a single week
    hols_of = {f: hols for f, _, _, hols, _ in parsed}
    for f, found, own, _, week in parsed:
        name = os.path.basename(f)
        missing = set()
        names = {**pooled, **global_legend, **own}
        for e in found:
            if e["faculty"]:
                e["faculty"] = resolve_faculty(e["faculty"], names, missing)
            e["rolls"], e["batch"], problem = map_batches(e["rolls"], e["time"], rules)
            if problem:
                batch_problems.setdefault(name, set()).add(problem)
            e["_src"] = name
        if target and found:
            has_terms = any(e["term"] for e in found)
            kept = [e for e in found if not e["term"] or e["term"] == target]
            if has_terms:
                print(f"{name}: {len(kept)} classes for term {target} "
                      f"({len(found) - len(kept)} from other terms skipped)")
            else:
                print(f"WARNING {name}: no term headings found, so all {len(found)} classes were kept. "
                      f"If this file covers several terms, check the heading wording.")
            found = kept
            if has_terms and not kept:
                print(f"WARNING {name}: no section for term {target} in this file.")
        else:
            hol_days = len([1 for x in hols_of.get(f, [])]) if not found else 0
            print(f"{name}: {len(found)} classes" if found else
                  f"{name}: {hol_days} holiday day(s)" if hol_days else
                  f"{name}: NOTHING FOUND, skipped (a scanned image inside a PDF? make a .csv instead)")
        if found and week:
            print(f"{name}: shows only in the week {week[0]} to {week[1]}")
            if (date.fromisoformat(week[1]) - date.fromisoformat(week[0])).days <= 6:
                weekly_files.append((monday_of(date.fromisoformat(week[0])), name))
        elif found and any(not e.get("from") for e in found):
            print(f"WARNING {name}: has no dates, so it repeats every week. Add a Date column, or put the "
                  f"week's start date in the file name (for example {os.path.splitext(name)[0]}_2026-09-21"
                  f"{os.path.splitext(name)[1]}).")
        if missing:
            unresolved[name] = sorted(missing)
        for e in found:
            key = json.dumps(e, sort_keys=True)
            if key not in seen:
                seen.add(key)
                entries.append(e)
    if clinic_files:
        rows, bad = load_clinics(clinic_files[0])
        for b in bad:
            print(f"WARNING clinics.csv {b}")
        if not rows:
            print("WARNING clinics.csv has no usable rows, so it was ignored and the timetable's own "
                  "clinic rows were left as they are.")
        else:
            entries, had_slots, cproblems = add_clinics(entries, rows, rules, target)
            lo, hi = min(r["from"] for r in rows), max(r["to"] for r in rows)
            print(f"clinics.csv: {len(rows)} postings from {lo} to {hi}, "
                  + ("placed in the clinic slots found in the timetable" if had_slots else
                     f"no clinic slot in the timetable, so used {DEFAULT_CLINIC_DAYS[0][:3]} to "
                     f"{DEFAULT_CLINIC_DAYS[-1][:3]} {DEFAULT_CLINIC_TIME} "
                     f"(add Days / Time columns to clinics.csv to change)"))
            for pr in sorted(cproblems):
                print(f"WARNING clinics.csv: {pr}")
    entries = apply_overrides(entries)
    # A day is a holiday when it's marked so (or listed with nothing under it) and no class falls on it.
    # A holiday with a date applies to that date only; one without repeats every week on that weekday.
    marked = {}
    for _, _, _, hols, _ in parsed:
        for h in hols:
            if target and h["term"] not in ("", target):
                continue
            iso = h.get("date", "")
            if iso:
                dd = date.fromisoformat(iso)
                if any(applies_on(e, dd) for e in entries if not e.get("rot")):
                    continue
            elif any(e["day"] == h["day"] and not e.get("from") for e in entries if not e.get("rot")):
                continue
            key = (iso, h["day"])
            old = marked.get(key)
            if old is None or (h["note"] and not old["note"]):
                marked[key] = {"day": h["day"], "note": h["note"], "date": iso, "explicit": h.get("explicit", False)}
    holidays = sorted(marked.values(), key=lambda h: (h["date"] or "0000", DAY_ORDER.index(h["day"])))
    # An explicit holiday cancels the clinics rotation on that date too.
    cancelled = {h["date"] for h in holidays if h["date"] and h["explicit"]}
    if cancelled:
        entries = [e for e in entries if not (e.get("rot") and e.get("from") in cancelled)]
    if holidays:
        print("Holidays: " + ", ".join((h["date"] + " " if h["date"] else "every ") + h["day"]
                                        + (f" ({h['note']})" if h["note"] else "") for h in holidays))
    holidays = [{k: v for k, v in h.items() if k != "explicit" and (v or k == "day")} for h in holidays]
    for name, problems in batch_problems.items():
        for pr in sorted(problems):
            print(f"WARNING {name}: {pr}. Those classes are shown to everyone until you add a rule "
                  f"for it in batches.json.")
    for name, codes in unresolved.items():
        print(f"WARNING {name}: no full name found for faculty code(s) {', '.join(codes)}. "
              f"Add them to faculty.csv (Code,Name).")
    if not entries:
        sys.exit("No classes found in any PDF. Each needs a table with a header row like "
                 "Day | Time | Class | Roll No | Location, and must not be a scanned image. "
                 "See README.md for the manual fallback.")
    for e in entries:
        e.pop("_src", None)

    # The site shows one week only: the newest week that has a weekly timetable (or TIMETABLE_WEEK).
    forced = parse_date(os.environ.get("TIMETABLE_WEEK", ""))
    if forced:
        wk_start, why = monday_of(date.fromisoformat(forced)), "set by TIMETABLE_WEEK"
    elif weekly_files:
        wk_start = max(w for w, _ in weekly_files)
        why = "the newest weekly timetable: " + ", ".join(sorted(n for w, n in weekly_files if w == wk_start))
    else:
        wk_start, why = monday_of(date.today()), "no weekly timetable found, so this week"
    wk_days = [wk_start + timedelta(days=i) for i in range(7)]
    wk_end = wk_days[-1]
    older = sorted(n for w, n in weekly_files if w < wk_start)
    before = len(entries)
    entries = [e for e in entries if any(applies_on(e, d) for d in wk_days)]
    holidays = [h for h in holidays if not h.get("date") or wk_start.isoformat() <= h["date"] <= wk_end.isoformat()]
    print(f"Showing the week {wk_start.isoformat()} to {wk_end.isoformat()} ({why})")
    if older:
        print("Older weeks are not shown (their files can stay in the repo): " + ", ".join(older))
    if not any(not e.get("rot") for e in entries):
        print("WARNING: no class in the timetable falls in that week, only the clinics rotation.")
    names = {}
    style = os.environ.get("TIMETABLE_NAMES", "full").strip().lower() or "full"
    if name_files and style not in ("off", "none", "no"):
        names, dupes = load_names(name_files[0], "first" if style == "first" else "full")
        print(f"names.csv: {len(names)} names, showing {'first names only' if style == 'first' else 'names as written'}"
              + (f". WARNING: {dupes} roll number(s) appear more than once, the last one was used" if dupes else ""))
    elif name_files:
        print("names.csv found but TIMETABLE_NAMES is off, so no names are published")
    data = {
        "title": os.environ.get("TIMETABLE_TITLE", "Class timetable"),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "term": target,
        "week": {"from": wk_start.isoformat(), "to": wk_end.isoformat()},
        "holidays": holidays,
        "names": names,
        "entries": entries,
    }
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    days = sorted({e["day"] for e in entries if e["day"] != "*"}, key=DAY_ORDER.index)
    print(f"Total: {len(entries)} classes in that week (of {before} found) across {len(days)} days: {', '.join(days)}")


if __name__ == "__main__":
    main()

