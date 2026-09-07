"""Time entity augmentation: based on jionlp time parsing, appends a readable
time annotation after time entities in the original text.

Examples:
    "I can go next Monday"      -> "I can go next Monday(2026-09-07)"
    "I'm free next Mon 7 to 9"  -> "I'm free next Mon 7 to 9(2026-09-07 07:00~09:00)"
    "from 7 to 9"               -> "from 7 to 9(07:00~09:00)"   # date omitted when it equals today
"""
from __future__ import annotations

import contextlib
import io
import time as _time
from datetime import datetime, timedelta
from typing import List, Optional

from jionlp.algorithm.ner import extract_time


def _fmt_clock(s: str) -> str:
    h, m, _ = s.split(":")
    return f"{h}:{m}" if m != "00" else h.zfill(2) + ":00"


def _fmt_end_clock(s: str) -> str:
    """End-of-range clock rendering: XX:59:59 is a closed interval written to
    the last second; carry over to the next whole hour."""
    if s[3:] == "59:59" and s[:2] != "23":
        return f"{int(s[:2]) + 1:02d}:00"
    return _fmt_clock(s)


def _same_day(a: str, b: str) -> bool:
    return a[:10] == b[:10]


def _whole_unit_date(start: str, end: str) -> Optional[str]:
    """When start/end exactly cover a whole day/month/year, return the shorthand
    at that granularity; otherwise return None."""
    if not (start.endswith("00:00:00") and end.endswith("23:59:59")):
        return None
    d0 = datetime.strptime(start[:10], "%Y-%m-%d")
    d1 = datetime.strptime(end[:10], "%Y-%m-%d")

    def _month_start(d: datetime) -> datetime:
        return d.replace(day=1)

    d0_ms = _month_start(d0) == d0
    d1_next = d1 + timedelta(days=1)
    d1_month_end = _month_start(d1_next) == d1_next

    if d0_ms and d1_month_end:  # full month or full-month range (starts at month begin, ends at month end)
        if d0.year == d1.year and (d0.month, d1.month) == (1, 12):
            return str(d0.year)  # full year
        m0, m1 = d0.strftime("%Y-%m"), d1.strftime("%Y-%m")
        return m0 if m0 == m1 else f"{m0}~{m1}"
    if d0 == d1:  # full day
        return d0.strftime("%Y-%m-%d")
    return None  # spanning days but not a full month: caller renders it as a date range


def _render(detail: dict, today: str) -> Optional[str]:
    """Render the jionlp parse result as the parenthesized annotation text;
    return None when it cannot be rendered."""
    times = detail.get("time")
    if not (isinstance(times, list) and len(times) == 2
            and all(isinstance(t, str) for t in times)):  # time_delta's time is a dict — skip
        return None
    start, end = times

    # full day/month/year -> abbreviated date by granularity
    whole = _whole_unit_date(start, end)
    if whole is not None:
        return whole
    if _same_day(start, end):
        day, today_omitted = start[:10], start[:10] == today
        # full hour (e.g. 15:00:00~15:59:59) -> annotate only 15:00
        if start[14:] == "00:00" and end[14:] == "59:59" and start[11:13] == end[11:13]:
            span = start[11:16]
        else:
            span = f"{_fmt_clock(start[11:])}~{_fmt_end_clock(end[11:])}"
        return span if today_omitted else f"{day} {span}"
    if start.endswith("00:00:00") and end.endswith("23:59:59"):  # spanning full days
        return f"{start[:10]}~{end[:10]}"
    # spanning days with clock times: keep the date on both sides; an end of
    # 23:59:59 carries over to 00:00 of the next day
    if end[11:] == "23:59:59":
        end = (datetime.strptime(end[:10], "%Y-%m-%d")
               + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    return f"{start[:10]} {_fmt_clock(start[11:])}~{end[:10]} {_fmt_end_clock(end[11:])}"


_RANGE_DELIMS = ("到", "至", "~", "～", "—", "-")


def _reanchor_end(text: str, start: str) -> Optional[str]:
    """Fix a jionlp parsing flaw where an elliptical range (e.g. "next Monday
    to Wednesday") parses to an end earlier than its start.

    Re-parses the right segment of the range anchored at the start time and
    takes its day as the new end date; returns None on failure.
    """
    idx = max((text.rfind(d) for d in _RANGE_DELIMS), default=-1)
    if idx <= 0 or idx >= len(text) - 1:
        return None
    tail = text[idx + 1:]
    anchor = _time.mktime(_time.strptime(start[:19], "%Y-%m-%d %H:%M:%S"))
    with contextlib.redirect_stdout(io.StringIO()):
        tail_ents = extract_time(tail, time_base=anchor)
    if not tail_ents:
        return None
    tail_time = tail_ents[0]["detail"].get("time")
    if not (isinstance(tail_time, list) and len(tail_time) == 2
            and all(isinstance(t, str) for t in tail_time)):
        return None
    new_end = tail_time[1]
    return new_end if new_end[:19] > start[:19] else None


def augment_time(
    text: str,
    time_base: Optional[float] = None,
) -> str:
    """Return the text with time annotations appended after time entities;
    return it unchanged when there is no time entity.

    Args:
        text: text to augment
        time_base: base timestamp for relative times (today / next week etc.);
            defaults to the current time
    """
    if time_base is None:
        time_base = _time.time()
    today = datetime.fromtimestamp(time_base).strftime("%Y-%m-%d")

    with contextlib.redirect_stdout(io.StringIO()):  # silence the WeChat official-account printout jionlp emits on its first call
        entities = extract_time(text, time_base=time_base)

    pieces: List[str] = []
    last = 0
    for ent in sorted(entities, key=lambda e: e["offset"][0]):
        s, e = ent["offset"]
        if s < last:  # skip overlapping entities
            continue
        detail = ent.get("detail", {})
        times = detail.get("time")
        if (isinstance(times, list) and len(times) == 2
                and all(isinstance(t, str) for t in times) and times[1][:19] < times[0][:19]):
            fixed = _reanchor_end(ent["text"], times[0])  # elliptical range correction
            if fixed is not None:
                detail = {**detail, "time": [times[0], fixed]}
        note = _render(detail, today)
        if note is None:
            continue
        pieces.append(text[last:s])
        pieces.append(f"{text[s:e]}({note})")
        last = e
    pieces.append(text[last:])
    return "".join(pieces)
