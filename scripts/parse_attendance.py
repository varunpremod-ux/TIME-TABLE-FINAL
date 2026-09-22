#!/usr/bin/env python3
"""Turn a cumulative attendance PDF into attendance.json.

Usage: python parse_attendance.py out/attendance.json <pdf-or-folder> [more ...]

The PDF lists Held / Attended / % for each subject, split into Theory and
Practical, plus the batch's own printed Total Theory / Total Practical /
overall Avg columns. The subjects and how many of them there are change
every term, so nothing here is hardcoded to "Community Medicine" or
"Forensic Medicine" - the three header rows on the first page are read at
parse time to work out how many subjects there are, what they're called,
and which columns belong to which subject's Theory/Practical/Held/Attd/%.
A later term with 3, 4 or 5 subjects works the same way.

Which rows are ours: same AFG-range filter as the mess bill (default
8058-8207, our J-3 range) - override with ATTENDANCE_AFG_MIN /
ATTENDANCE_AFG_MAX if the batch's AFG range ever changes. This also
naturally drops any stray extra row a PDF sometimes has tacked on past our
own batch (their AFG falls outside our range), without needing to hardcode
a roll number to skip.

Roll numbers: taken directly from the PDF's own "Ser No" column (it already
numbers our batch 1-145 plus 146-150 for our five batchmates studying under
J-3 in the foreign-students table elsewhere), not re-derived.

Thresholds: the theory and practical minimums (75% / 80%) are written into
the JSON as ATTENDANCE_THEORY_MIN / ATTENDANCE_PRACTICAL_MIN so the site can
show pass/fail and "how many more to attend" without the numbers being
baked into the page - override via those two env vars if the minimums ever
change. ATTENDANCE_AS_OF overrides the "up to" date read from the PDF.

Most recent report only: these are cumulative reports, so a newer one fully
replaces an older one rather than adding to it. If more than one attendance
PDF is sitting in the repo, only the one with the latest "up to" date is
used; older ones are reported and skipped, the same way the timetable keeps
only its newest week.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import pdfplumber


def clean(cell):
    return re.sub(r"\s+", " ", (cell or "")).strip()


def to_int(v):
    v = clean(v)
    return int(v) if re.fullmatch(r"-?\d+", v) else 0


def is_header_start(row):
    return len(row) > 2 and clean(row[2]).upper() == "NAME"


def build_header(row0, row1, row2):
    n = len(row0)
    subj = [None] * n
    last = None
    for i, v in enumerate(row0):
        c = clean(v)
        if c:
            last = c.upper()
        subj[i] = last
    typ = [None] * n
    last = None
    for i, v in enumerate(row1):
        c = clean(v)
        if c:
            last = c.upper()
        typ[i] = last
    return subj, typ


def column_groups(subj, typ, n):
    """Chunk columns 3..n-2 into runs sharing the same (subject, theory/practical),
    in the order they appear. Column n-1 (the overall avg, subject "TOT") is not
    included here - it's read separately."""
    groups = []
    cur_key, cur_cols = None, []
    for i in range(3, n - 1):
        key = (subj[i], typ[i])
        if key != cur_key:
            if cur_cols:
                groups.append((cur_key[0], cur_key[1], cur_cols))
            cur_key, cur_cols = key, [i]
        else:
            cur_cols.append(i)
    if cur_cols:
        groups.append((cur_key[0], cur_key[1], cur_cols))
    return groups


def block_from_cols(row, cols):
    vals = [to_int(row[c]) for c in cols]
    held = vals[0] if len(vals) > 0 else 0
    attd = vals[1] if len(vals) > 1 else 0
    pct = vals[2] if len(vals) > 2 else (round(attd / held * 100) if held else 0)
    return {"held": held, "attd": attd, "pct": pct}


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        tables = [t for page in pdf.pages for t in page.extract_tables()]
    return first_text, tables


def extract_records(tables):
    header = None
    records = []
    for t in tables:
        if not t:
            continue
        body = t
        if is_header_start(t[0]) and len(t) >= 3:
            header = build_header(t[0], t[1], t[2])
            body = t[3:]
        if not header:
            continue
        subj, typ = header
        n = len(subj)
        groups = column_groups(subj, typ, n)
        for row in body:
            if len(row) != n:
                continue
            roll = clean(row[0])
            afg = clean(row[1])
            if not re.fullmatch(r"\d{1,4}", roll) or not re.fullmatch(r"\d{3,5}", afg):
                continue
            name = clean(row[2]).title()
            subjects = {}
            total_theory, total_practical = None, None
            for gsubj, gtyp, cols in groups:
                block = block_from_cols(row, cols)
                if gsubj == "TOTAL":
                    if gtyp == "THEORY":
                        total_theory = block
                    elif gtyp == "PRACTICAL":
                        total_practical = block
                elif gsubj:
                    label = gsubj.title()
                    subjects.setdefault(label, {})[(gtyp or "theory").lower()] = block
            avg_pct = to_int(row[n - 1]) if n else 0
            records.append({
                "roll": roll, "afg": afg, "name": name,
                "subjects": subjects,
                "total_theory": total_theory,
                "total_practical": total_practical,
                "avg_pct": avg_pct,
            })
    return records


def as_of_from_text(text):
    m = re.search(r"UP TO\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text or "", re.I)
    return m.group(1).title() if m else None


def as_of_sort_key(as_of):
    """'31 May 2026' -> a comparable date; unparseable/missing sorts lowest."""
    if not as_of:
        return datetime.min
    try:
        return datetime.strptime(as_of, "%d %B %Y")
    except ValueError:
        return datetime.min


def term_from_text(text):
    m = re.search(r",\s*([IVX]+)\s+TERM", text or "", re.I)
    return f"{m.group(1).upper()} Term" if m else None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dst = sys.argv[1]
    args = sys.argv[2:]
    files = []
    for a in args:
        if os.path.isdir(a):
            files += [os.path.join(a, f) for f in sorted(os.listdir(a)) if f.lower().endswith(".pdf")]
        elif os.path.exists(a):
            files.append(a)
    if not files:
        sys.exit("No attendance PDF found.")

    afg_min = int(os.environ.get("ATTENDANCE_AFG_MIN", "8058"))
    afg_max = int(os.environ.get("ATTENDANCE_AFG_MAX", "8207"))

    parsed = [(f, *parse_pdf(f)) for f in files]  # (file, text, tables)
    dated = [(f, text, tables, as_of_from_text(text)) for f, text, tables in parsed]

    # Cumulative reports supersede one another, so only one file is used - the
    # latest "up to" date; if two files somehow tie on that date (e.g. a
    # re-uploaded correction), the most recently modified one wins.
    best_key = max(as_of_sort_key(a) for _, _, _, a in dated)
    tied = [(f, text, tables, a) for f, text, tables, a in dated if as_of_sort_key(a) == best_key]
    use_file, use_text, use_tables, as_of = max(tied, key=lambda x: os.path.getmtime(x[0]))
    for f, _, _, a in dated:
        if f != use_file:
            reason = "older report" if as_of_sort_key(a) != best_key else "same date, older file"
            print(f"{os.path.basename(f)}: {reason}, skipped (delete it from the repo when convenient)")

    term = term_from_text(use_text)
    all_records = extract_records(use_tables)
    print(f"{os.path.basename(use_file)}: {len(all_records)} rows read from the PDF's tables")

    ours = [r for r in all_records if afg_min <= int(r["afg"]) <= afg_max]
    ours.sort(key=lambda r: int(r["afg"]))
    if not ours:
        sys.exit(f"No rows found with AFG between {afg_min} and {afg_max}. "
                  f"Check ATTENDANCE_AFG_MIN / ATTENDANCE_AFG_MAX in deploy.yml.")

    # self-consistency check: per-subject theory/practical held+attd should sum to
    # the PDF's own printed total columns. Mismatches are reported, not fatal.
    mismatches = 0
    for r in ours:
        for kind, total in (("theory", r["total_theory"]), ("practical", r["total_practical"])):
            if not total:
                continue
            held_sum = sum(s[kind]["held"] for s in r["subjects"].values() if kind in s)
            attd_sum = sum(s[kind]["attd"] for s in r["subjects"].values() if kind in s)
            if held_sum != total["held"] or attd_sum != total["attd"]:
                mismatches += 1
    if mismatches:
        print(f"warning: {mismatches} subject-sum vs printed-total mismatches "
              f"(check the PDF's column layout)")

    subjects_seen = []
    for r in ours:
        for name in r["subjects"]:
            if name not in subjects_seen:
                subjects_seen.append(name)

    students = {r["roll"]: {
        "roll": r["roll"], "afg": r["afg"], "name": r["name"],
        "subjects": r["subjects"],
        "total_theory": r["total_theory"],
        "total_practical": r["total_practical"],
        "avg_pct": r["avg_pct"],
    } for r in ours}

    data = {
        "title": "Attendance",
        "as_of": os.environ.get("ATTENDANCE_AS_OF", "") or as_of or "",
        "term": term or "",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "theory_min_pct": int(os.environ.get("ATTENDANCE_THEORY_MIN", "75")),
        "practical_min_pct": int(os.environ.get("ATTENDANCE_PRACTICAL_MIN", "80")),
        "subjects": subjects_seen,
        "students": students,
    }
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"{data['as_of'] or 'attendance'}: {len(students)} students (AFG {afg_min}-{afg_max}), "
          f"subjects: {', '.join(subjects_seen)}")


if __name__ == "__main__":
    main()
