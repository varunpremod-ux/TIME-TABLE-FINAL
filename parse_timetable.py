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

Batches: when the Roll No column holds batch letters (A, B, C, D) instead of numbers,
they're converted to roll-number ranges using the class start time, because the batch
split differs by time of day. The split is set in DEFAULT_BATCH_RULES below, and can be
changed without touching code by adding a batches.json file (see README).

Holidays: a day marked "Holiday" (or "No classes") in the timetable, or a day that is
listed with nothing written under it, is saved as a holiday and shown as one on the site.

Faculty codes: if the Faculty column holds short codes (AB, AKS) and the PDF lists
the full names somewhere (usually at the end: "AB - Dr. Anil Bose"), the codes are
replaced by the full names, using the key from the same file first, then faculty.csv,
then keys found in your other files. A file called faculty.csv (columns: Code,Name)
can add names that aren't listed in any timetable file.
"""
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

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
    s = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", s)
    s = re.sub(r"(?<![\d:.])([01]?\d|2[0-3])([0-5]\d)(?![\d:.])", r"\1:\2", s)  # 1030 -> 10:30
    return re.sub(r"\s*-\s*", " - ", s).strip()


def norm_rolls(s):
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\bto\b", "-", s, flags=re.I).strip()


# ---------- batches -> roll ranges ----------
# Classes starting from `start_from` until `start_before` (24-hour HH:MM) use that split.
DEFAULT_BATCH_RULES = [
    {"start_from": "10:00", "start_before": "14:00",
     "batches": {"A": "1-37", "B": "38-76", "C": "77-114", "D": "115-150"}},
    {"start_from": "14:00", "start_before": "24:00",
     "batches": {"A": "1-50", "B": "51-100", "C": "101-150"}},
]
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
        for a, b, single in re.findall(r"(\d+)\s*-\s*(\d+)|(\d+)", spec):
            spans.append((int(a), int(b)) if a else (int(single), int(single)))
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in merged)


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
    holidays, days_seen = [], set()
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
                  if c and c is not hol and colmap.get(i) not in ("day", "term", "rolls")
                  and not DAY_ONLY.match(c) and not term_of_line(c)]
        if hol and not others:
            day = ((norm_day(hol) if DAY_PREFIX.match(hol) else None)
                   or next((norm_day(c) for c in filled if DAY_ONLY.match(c)), None)
                   or (norm_day(row[day_idx]) if day_idx is not None and day_idx < len(row) and row[day_idx] else None)
                   or carry.get("day"))
            if day:
                term_idx = next((i for i, f in colmap.items() if f == "term"), None)
                row_term = norm_term(row[term_idx]) if term_idx is not None and term_idx < len(row) else ""
                holidays.append({"term": row_term or term, "day": day, "note": holiday_note(hol)})
                continue

        if len(filled) == 1 and DAY_ONLY.match(filled[0]):
            carry = {"day": norm_day(filled[0])}
            days_seen.add((term, carry["day"]))  # a day with nothing under it turns out to be a holiday
            continue

        # A row whose day cell isn't a weekday is not a class (e.g. a faculty list row).
        if day_idx is not None and day_idx < len(row) and row[day_idx] and not norm_day(row[day_idx]):
            legend_from_cells(row, legend)
            continue

        rec = {}
        for idx, field in colmap.items():
            val = row[idx] if idx < len(row) else ""
            if field == "day" and val:
                val = norm_day(val) or ""
                if val and val != carry.get("day"):
                    carry = {"day": val}
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

        for field in CARRY:
            if field in colmap.values():
                if rec.get(field):
                    carry[field] = rec[field]
                else:
                    rec[field] = carry.get(field, "")
        rec["subject"] = rec.get("subject") or default_subject
        if not rec.get("day"):
            continue
        entries.append({
            "term": rec.get("term") or term,
            "day": rec["day"],
            "time": rec.get("time", ""),
            "class": rec.get("class", ""),
            "subject": rec["subject"],
            "faculty": rec.get("faculty", ""),
            "rolls": rec.get("rolls") or "All",
            "batch": "",
            "location": rec.get("location", ""),
        })

    if not is_csv:
        legend_from_text(path, legend)
    have_classes = {(e["term"], e["day"]) for e in entries}
    for t, d in sorted(days_seen - have_classes):
        holidays.append({"term": t, "day": d, "note": ""})
    return entries, legend, holidays


# ---------- main ----------

SUPPORTED = (".pdf", ".csv")
IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".heic")
LEGEND_FILE = "faculty.csv"
BATCH_FILE = "batches.json"


def collect(args):
    files, legend_files, batch_files = [], [], []
    for a in args:
        if os.path.isdir(a):
            for f in sorted(os.listdir(a)):
                low, full = f.lower(), os.path.join(a, f)
                if low == LEGEND_FILE:
                    legend_files.append(full)
                elif low == BATCH_FILE:
                    batch_files.append(full)
                elif low.endswith(SUPPORTED):
                    files.append(full)
                elif low.endswith(IMAGES):
                    print(f"{f}: IGNORED. Photos can't be read automatically. "
                          f"Turn it into a .csv (see README) and upload that instead.")
        elif os.path.exists(a):
            files.append(a)
        else:
            sys.exit(f"Can't find {a}")
    return files, legend_files, batch_files


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dst = sys.argv[1]
    files, legend_files, batch_files = collect(sys.argv[2:])
    rules = load_batch_rules(batch_files[0]) if batch_files else DEFAULT_BATCH_RULES
    print(f"Batch rules: {'batches.json' if batch_files else 'built-in defaults'}")
    if not files:
        sys.exit("No PDF or CSV files found. Upload your timetables to the repo's main page.")
    global_legend = {}
    for lf in legend_files:
        global_legend.update(load_legend_csv(lf))
        print(f"{os.path.basename(lf)}: {len(global_legend)} faculty names")

    target = norm_term(os.environ.get("TIMETABLE_TERM", ""))
    parsed = [(f, *parse(f)) for f in files]  # (file, entries, own faculty key, holidays)
    # Faculty keys can sit in a different file from the classes that use them.
    pooled = {}
    for _, _, lg, _ in parsed:
        for k, v in lg.items():
            pooled.setdefault(k, v)

    entries, seen, unresolved, batch_problems = [], set(), {}, {}
    for f, found, own, _ in parsed:
        name = os.path.basename(f)
        missing = set()
        names = {**pooled, **global_legend, **own}
        for e in found:
            if e["faculty"]:
                e["faculty"] = resolve_faculty(e["faculty"], names, missing)
            e["rolls"], e["batch"], problem = map_batches(e["rolls"], e["time"], rules)
            if problem:
                batch_problems.setdefault(name, set()).add(problem)
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
            print(f"{name}: {len(found)} classes" if found else
                  f"{name}: NOTHING FOUND, skipped (a scanned image inside a PDF? make a .csv instead)")
        if missing:
            unresolved[name] = sorted(missing)
        for e in found:
            key = tuple(e.values())
            if key not in seen:
                seen.add(key)
                entries.append(e)
    # A day is a holiday when it's marked so (or left empty) and no class falls on it.
    class_days = {e["day"] for e in entries}
    marked = {}
    for _, _, _, hols in parsed:
        for h in hols:
            if target and h["term"] not in ("", target):
                continue
            if h["day"] not in class_days and (h["day"] not in marked or (h["note"] and not marked[h["day"]])):
                marked[h["day"]] = h["note"]
    holidays = [{"day": d, "note": marked[d]} for d in DAY_ORDER if d in marked]
    if holidays:
        print("Holidays: " + ", ".join(h["day"] + (f" ({h['note']})" if h["note"] else "") for h in holidays))
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
    data = {
        "title": os.environ.get("TIMETABLE_TITLE", "Class timetable"),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "term": target,
        "holidays": holidays,
        "entries": entries,
    }
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    days = sorted({e["day"] for e in entries})
    print(f"Total: {len(entries)} classes across {len(days)} days: {', '.join(days)}")


if __name__ == "__main__":
    main()
