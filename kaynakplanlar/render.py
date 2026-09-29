"""Output rendering: human table, per-hour load chart and the JSON document.

Pure functions only -- no printing, no exiting.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .model import Window
from .schedule import Placement, ScheduleResult, Warning

__all__ = [
    "render_table",
    "render_load",
    "render_warnings",
    "render_summary",
    "build_json",
    "dumps_json",
    "load_rows",
    "bar_for",
    "DEFAULT_BAR_WIDTH",
]

DEFAULT_BAR_WIDTH = 20
BAR_CHAR = "█"

HEADERS = ("start_iso", "end_iso", "id", "run#", "priority", "deps")
_PLACEHOLDER = "-"


# --------------------------------------------------------------------------
# table
# --------------------------------------------------------------------------


def _cells(placement: Placement) -> Tuple[str, ...]:
    return (
        placement.start.isoformat(),
        placement.end.isoformat(),
        placement.id,
        str(placement.run),
        str(placement.priority),
        ", ".join(placement.deps) if placement.deps else _PLACEHOLDER,
    )


def render_table(placements: Sequence[Placement]) -> str:
    """Render the schedule as a fixed-width table."""
    rows = [_cells(p) for p in placements]
    widths = [len(h) for h in HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    out = [line(HEADERS), "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in rows)
    if not rows:
        out = [HEADERS[0], _PLACEHOLDER]
    return "\n".join(out)


# --------------------------------------------------------------------------
# warnings / summary
# --------------------------------------------------------------------------


def render_warnings(warnings: Sequence[Warning]) -> List[str]:
    """``PUSHED id=... run=... from=... to=... reason=collision`` style lines."""
    return [w.line() for w in warnings]


def render_summary(summary: Dict[str, int]) -> str:
    return " ".join(
        "%s=%d" % (key, summary[key]) for key in ("jobs", "runs", "dropped", "blocked", "pushed")
    )


# --------------------------------------------------------------------------
# load histogram
# --------------------------------------------------------------------------


def load_rows(
    window: Window,
    placements: Sequence[Placement],
) -> List[Tuple[datetime, float]]:
    """Minutes scheduled per clock hour, covering every hour of the window.

    Minutes are split at hour boundaries, so a 90 minute placement starting at
    23:45 contributes 15 minutes to the first hour and 60 to the second.
    """
    buckets: Dict[datetime, float] = {}
    for placement in placements:
        moment = placement.start
        while moment < placement.end:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            segment_end = min(placement.end, hour + timedelta(hours=1))
            span = (segment_end - moment).total_seconds() / 60.0
            buckets[hour] = buckets.get(hour, 0.0) + span
            moment = segment_end

    rows: List[Tuple[datetime, float]] = []
    cursor = window.start.replace(minute=0, second=0, microsecond=0)
    while cursor < window.end:  # only bins intersecting the half-open window
        rows.append((cursor, buckets.get(cursor, 0.0)))
        cursor += timedelta(hours=1)
    return rows


def bar_for(minutes: float, busiest: float, width: int = DEFAULT_BAR_WIDTH) -> str:
    """Scale ``minutes`` against the busiest hour onto ``width`` bar cells."""
    if minutes <= 0 or busiest <= 0 or width <= 0:
        return ""
    cells = int(round(minutes * width / busiest))
    cells = max(1, min(width, cells))
    return BAR_CHAR * cells


def render_load(
    window: Window,
    placements: Sequence[Placement],
    width: int = DEFAULT_BAR_WIDTH,
) -> str:
    rows = load_rows(window, placements)
    busiest = max((minutes for _, minutes in rows), default=0.0)
    label_width = max((len(hour.isoformat()) for hour, _ in rows), default=0)
    minute_width = max((len(str(int(round(m)))) for _, m in rows), default=0)

    lines = ["load (scheduled minutes per hour)"]
    for hour, minutes in rows:
        bar = bar_for(minutes, busiest, width)
        lines.append(
            "%s  %s  %s"
            % (hour.isoformat().ljust(label_width),
               str(int(round(minutes))).rjust(minute_width),
               bar)
        )
    lines.append("busiest hour: %d min" % int(round(busiest)))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# json
# --------------------------------------------------------------------------


def build_json(
    result: ScheduleResult,
    window: Optional[Window] = None,
    placements: Optional[Sequence[Placement]] = None,
    bar_width: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the machine-readable document.

    ``placements``/``bar_width`` add the ``load`` block when supplied.
    """
    payload: Dict[str, Any] = {
        "schedule": [p.as_dict() for p in result.schedule],
        "warnings": [w.as_dict() for w in result.warnings],
        "summary": result.summary(),
    }
    if window is not None and placements is not None:
        rows = load_rows(window, placements)
        busiest = max((minutes for _, minutes in rows), default=0.0)
        width = DEFAULT_BAR_WIDTH if bar_width is None else bar_width
        payload["load"] = [
            {
                "hour": hour.isoformat(),
                "minutes": int(round(minutes)),
                "bar": bar_for(minutes, busiest, width),
            }
            for hour, minutes in rows
        ]
    return payload


def dumps_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
