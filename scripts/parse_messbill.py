#!/usr/bin/env python3
"""Turn a mess bill PDF into messbill.json.

Usage: python parse_messbill.py out/messbill.json <pdf-or-folder> [more ...]

Give it one or more PDFs (or a folder containing one), and the mess bill for
our batch is extracted and written out. Every batch's mess bill (I-3, J-3,
K-3, L-3, and a separate Foreign Students table) is normally printed in one
PDF; only the rows belonging to our own batch are kept.

Which rows are ours: set by AFG number range, since that's what's stable
across a term. Defaults to J-3 (8058-8207, the 145 J-3 cadets plus their
5 foreign batchmates). Override with the environment variables
MESSBILL_AFG_MIN / MESSBILL_AFG_MAX if the batch's AFG range is ever
different (for example a new intake year).

Roll numbers: rows are sorted by AFG number and numbered 1, 2, 3... in that
order, matching the mess bill's own Ser No for the main batch table and
continuing the sequence for the foreign students listed separately.

Two table shapes: the main batch table (Est Chgs / Wages / Extra messing /
House keeping / Washing / Hair Cutting / Parents Messing / Cades Room Upkeep
/ Bio-metric Fine / Discipline Fine / Mess bill Late / Arrears / Total) and
the Foreign Students table (Days / Extra messing / Wages / Est / House
Keeping / Mess Maintenance / Washing / Hair cutting / Fine / Parents Messing
/ Cades Room Upkeep / Arrears / Total) are both read directly from each
table's own header row, so column order/wording differences between the two
don't matter, and a still-different layout next month is handled the same
way as long as it has its own header row.

Normal vs Additional: each student's charges are split into two groups.
Normal Charges are the flat monthly costs (Establishment, Wages, Extra
Messing, House Keeping, Washing, Hair Cutting, Mess Maintenance where it
applies). Additional Charges are the occasional, per-student ones (Parents
Messing, Cadets' Room Upkeep, Bio-metric Fine, Discipline Fine, Mess Bill
Late, Arrears, or Fine for foreign students) - only the ones that are
actually non-zero for that student are listed, so the site can show
"No additional charges" rather than a wall of zeros.

Month: read from the PDF's own heading ("MESS BILL FOR THE MONTH OF AUG
2026"). MESSBILL_MONTH=September 2026 in deploy.yml can override it.

One month at a time: if more than one mess bill PDF is sitting in the repo (an
old month never got deleted), only the PDF(s) for the newest month - by the
month printed in the PDF itself, not the file name - are used; older ones are
reported and skipped, the same way the timetable keeps only its newest week.
"""
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import pdfplumber

MONTHS = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "may": "May", "jun": "June", "jul": "July", "aug": "August",
    "sep": "September", "sept": "September", "oct": "October",
    "nov": "November", "dec": "December",
}


def clean(cell):
    return re.sub(r"\s+", " ", (cell or "")).strip()


HEADER_WORDS = {
    "ser": ["ser", "s.n"],
    "afg": ["afg"],
    "acno": ["a/c", "ac/no"],
    "name": ["name"],
    "days": ["days"],
    "est": ["est"],
    "wages": ["wages"],
    "extra_messing": ["extra"],
    "housekeeping": ["house"],
    "mess_maintenance": ["mess", "maint"],  # checked before 'mess bill late' below via order
    "washing": ["wash"],
    "haircut": ["hair"],
    "fine": ["fine"],
    "parents_messing": ["parent"],
    "cades_room_upkeep": ["cade"],
    "biometric_fine": ["bio"],
    "discipline_fine": ["discipl"],
    "messbill_late": ["late"],
    "arrears": ["arrear"],
    "total": ["total"],
}
# Order matters: more specific checks first (e.g. "mess...late" beats "mess maintenance").
HEADER_ORDER = ["ser", "afg", "acno", "name", "days", "messbill_late", "mess_maintenance",
                "est", "wages", "extra_messing", "housekeeping", "washing", "haircut",
                "fine", "parents_messing", "cades_room_upkeep", "biometric_fine",
                "discipline_fine", "arrears", "total"]


def normalize_header(h):
    h = clean(h).lower()
    if h in ("ser no", "s.n o", "s.no", "sno"):
        return "ser"
    if "mess" in h and "bill" in h:
        return "messbill_late"
    if h == "late":
        return "messbill_late"
    if "mess" in h and "maint" in h:
        return "mess_maintenance"
    for field in HEADER_ORDER:
        if field in ("ser", "messbill_late", "mess_maintenance"):
            continue
        for word in HEADER_WORDS[field]:
            if word in h:
                return field
    return h


def is_header_row(row):
    if len(row) < 2:
        return False
    c0, c1 = clean(row[0]).lower(), clean(row[1]).lower()
    return c0 in ("ser no", "s.n o", "s.no") and "afg" in c1


NORMAL_FIELDS = ["est", "wages", "extra_messing", "housekeeping", "washing", "haircut", "mess_maintenance"]
ADDITIONAL_FIELDS = ["parents_messing", "cades_room_upkeep", "biometric_fine",
                      "discipline_fine", "messbill_late", "arrears", "fine"]
LABELS = {
    "est": "Establishment Charges",
    "wages": "Wages (Civilian Worker)",
    "extra_messing": "Extra Messing",
    "housekeeping": "House Keeping",
    "washing": "Washing Service",
    "haircut": "Hair Cutting Service",
    "mess_maintenance": "Mess Maintenance",
    "parents_messing": "Parents Messing",
    "cades_room_upkeep": "Cadets' Room Upkeep",
    "biometric_fine": "Bio-metric Fine",
    "discipline_fine": "Discipline Fine",
    "messbill_late": "Mess Bill - Late Payment",
    "arrears": "Arrears",
    "fine": "Fine",
}


def to_int(v):
    v = clean(v)
    return int(v) if re.fullmatch(r"-?\d+", v) else 0


def month_from_text(text):
    m = re.search(r"MONTH OF\s+([A-Za-z]{3,9})\.?\s+(\d{4})", text or "", re.I)
    if not m:
        return None
    name = MONTHS.get(m.group(1).lower()[:4].rstrip(".")[:3], None) or MONTHS.get(m.group(1).lower()[:3])
    if not name:
        return None
    return f"{name} {m.group(2)}"


MONTH_NUM = {name: i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def month_sort_key(month):
    """'August 2026' -> 24320 (year*12+month), sortable; unknown months sort lowest."""
    if not month:
        return -1
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", month)
    if not m or m.group(1) not in MONTH_NUM:
        return -1
    return int(m.group(2)) * 12 + MONTH_NUM[m.group(1)]


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        tables = [t for page in pdf.pages for t in page.extract_tables()]
    return first_text, tables


def extract_rows(tables):
    header = None
    rows = []
    for t in tables:
        if not t:
            continue
        body = t
        if is_header_row(t[0]):
            header = [normalize_header(c) for c in t[0]]
            body = t[1:]
        if not header or "afg" not in header:
            continue
        afg_idx = header.index("afg")
        for row in body:
            if len(row) != len(header):
                continue
            afgval = clean(row[afg_idx])
            if not re.fullmatch(r"\d{3,5}", afgval):
                continue
            rec = {}
            for h, v in zip(header, row):
                rec[h] = clean(v)
            rows.append(rec)
    return rows


def build_student(rec):
    normal = []
    normal_total = 0
    for f in NORMAL_FIELDS:
        if f in rec:
            amt = to_int(rec[f])
            normal_total += amt
            normal.append({"label": LABELS[f], "amount": amt})
    additional = []
    additional_total = 0
    for f in ADDITIONAL_FIELDS:
        if f in rec:
            amt = to_int(rec[f])
            if amt:
                additional_total += amt
                additional.append({"label": LABELS[f], "amount": amt})
    total = to_int(rec.get("total", "0")) or (normal_total + additional_total)
    out = {
        "name": rec.get("name", ""),
        "afg": rec.get("afg", ""),
        "normal": normal,
        "normal_total": normal_total,
        "additional": additional,
        "additional_total": additional_total,
        "total": total,
    }
    if rec.get("days"):
        out["days_present"] = to_int(rec["days"])
    return out


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
        sys.exit("No mess bill PDF found.")

    afg_min = int(os.environ.get("MESSBILL_AFG_MIN", "8058"))
    afg_max = int(os.environ.get("MESSBILL_AFG_MAX", "8207"))

    parsed = []  # (file, month, tables)
    for f in files:
        text, tables = parse_pdf(f)
        parsed.append((f, month_from_text(text), tables))

    # Only one file is used - the newest month; if two files somehow tie on month
    # (e.g. a re-uploaded correction), the most recently modified one wins - so an
    # old month, or an old copy of the same month, never gets merged into the bill.
    best_key = max(month_sort_key(m) for _, m, _ in parsed)
    tied = [(f, m, t) for f, m, t in parsed if month_sort_key(m) == best_key]
    use_file, month, use_tables = max(tied, key=lambda ft: os.path.getmtime(ft[0]))
    for f, m, t in parsed:
        if f != use_file:
            reason = "older month" if month_sort_key(m) != best_key else "same month, older file"
            print(f"{os.path.basename(f)}: {reason}, skipped (delete it from the repo when convenient)")

    all_rows = extract_rows(use_tables)
    print(f"{os.path.basename(use_file)}: {len(all_rows)} rows read from the PDF's tables")

    ours = [r for r in all_rows if r.get("afg", "").isdigit() and afg_min <= int(r["afg"]) <= afg_max]
    ours.sort(key=lambda r: int(r["afg"]))
    if not ours:
        sys.exit(f"No rows found with AFG between {afg_min} and {afg_max}. "
                  f"Check MESSBILL_AFG_MIN / MESSBILL_AFG_MAX in deploy.yml.")

    students = {}
    for i, rec in enumerate(ours, start=1):
        students[str(i)] = build_student(rec)

    month = os.environ.get("MESSBILL_MONTH", "") or month or "This month"
    data = {
        "title": "Mess Bill",
        "month": month,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "students": students,
    }
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    total_sum = sum(s["total"] for s in students.values())
    print(f"{month}: {len(students)} students (AFG {afg_min}-{afg_max}), "
          f"total billed {total_sum}")


if __name__ == "__main__":
    main()
