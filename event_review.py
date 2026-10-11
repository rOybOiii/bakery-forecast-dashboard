"""Manager-facing event review contracts for the session-backed MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Iterable, Mapping


@dataclass(frozen=True)
class EventSegment:
    name: str
    start_date: date
    end_date: date
    interpretation: str = "Phase"

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


DEFAULT_EVENT_METADATA: dict[str, dict[str, object]] = {
    "downtown_0_020": {
        "suggested_name": "Payday weekend",
        "family": "Payday weekend",
        "category": "Calendar",
        "tags": ("recurring", "weekend", "positive-lift"),
        "status": "Needs review",
    },
    "downtown_0_019": {
        "suggested_name": "Winter disruption",
        "family": "Weather disruption",
        "category": "Weather",
        "tags": ("negative-lift", "multi-day", "winter"),
        "status": "Needs review",
    },
    "downtown_0_013": {
        "suggested_name": "Downtown Arts Walk",
        "family": "Downtown Arts Walk",
        "category": "Local/community",
        "tags": ("recurring", "downtown", "multi-day"),
        "status": "Needs review",
    },
    "downtown_0_011": {
        "suggested_name": "Campus break",
        "family": "Campus calendar disruption",
        "category": "School/sports",
        "tags": ("negative-lift", "campus", "multi-day"),
        "status": "Needs review",
    },
    "downtown_0_007": {
        "suggested_name": "Farmers market opening",
        "family": "Farmers market",
        "category": "Local/community",
        "tags": ("downtown", "weekend", "positive-lift"),
        "status": "Needs review",
    },
}


FAMILY_HISTORY: dict[str, tuple[dict[str, object], ...]] = {
    "Payday weekend": (
        {"label": "Payday weekend · Jan 2025", "peak_percent": 8.4, "duration_days": 3},
        {"label": "Payday weekend · Dec 2024", "peak_percent": 7.8, "duration_days": 3},
    ),
    "Downtown Arts Walk": (
        {"label": "Downtown Arts Walk · 2023", "peak_percent": 8.9, "duration_days": 6},
        {"label": "Downtown Arts Walk · 2022", "peak_percent": 7.5, "duration_days": 5},
    ),
    "Farmers market": (
        {"label": "Farmers market opening · 2023", "peak_percent": 8.7, "duration_days": 5},
    ),
}


def default_annotation(
    disturbance_id: str,
    *,
    direction: str,
) -> dict[str, object]:
    metadata = DEFAULT_EVENT_METADATA.get(disturbance_id, {})
    suggested_name = str(metadata.get("suggested_name", "Unnamed detection"))
    return {
        "name": suggested_name,
        "suggested_name": suggested_name,
        "family": str(metadata.get("family", "Unassigned")),
        "category": str(metadata.get("category", "Unclassified")),
        "tags": tuple(metadata.get("tags", (f"{direction}-lift",))),
        "status": str(metadata.get("status", "Needs review")),
        "notes": "",
        "segments": (),
    }


def default_segments(start_date: date, end_date: date) -> tuple[EventSegment, ...]:
    """Create contiguous buildup, peak, and tail phases across an event island."""

    duration = (end_date - start_date).days + 1
    if duration < 1:
        raise ValueError("Event end date must not precede its start date")
    if duration == 1:
        return (EventSegment("Peak", start_date, end_date),)
    if duration == 2:
        return (
            EventSegment("Buildup", start_date, start_date),
            EventSegment("Peak", end_date, end_date),
        )

    edge = max(1, duration // 3)
    buildup_end = start_date + timedelta(days=edge - 1)
    tail_start = end_date - timedelta(days=edge - 1)
    return (
        EventSegment("Buildup", start_date, buildup_end),
        EventSegment("Peak", buildup_end + timedelta(days=1), tail_start - timedelta(days=1)),
        EventSegment("Tail", tail_start, end_date),
    )


def validate_segments(
    segments: Iterable[EventSegment],
    *,
    event_start: date,
    event_end: date,
) -> tuple[str, ...]:
    ordered = sorted(segments, key=lambda segment: (segment.start_date, segment.end_date))
    errors: list[str] = []
    for segment in ordered:
        if not segment.name.strip():
            errors.append("Every segment needs a name.")
        if segment.start_date > segment.end_date:
            errors.append(f"{segment.name or 'A segment'} ends before it starts.")
        if segment.start_date < event_start or segment.end_date > event_end:
            errors.append(
                f"{segment.name or 'A segment'} falls outside the original model detection."
            )
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_date <= previous.end_date:
            errors.append(f"{previous.name} and {current.name} overlap.")
    return tuple(dict.fromkeys(errors))


def family_summary(
    family: str,
    occurrences: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    rows = list(occurrences) + list(FAMILY_HISTORY.get(family, ()))
    peaks = [float(row["peak_percent"]) for row in rows]
    durations = [int(row["duration_days"]) for row in rows]
    return {
        "family": family,
        "occurrence_count": len(rows),
        "typical_peak_percent": median(peaks) if peaks else 0.0,
        "typical_duration_days": median(durations) if durations else 0.0,
    }
