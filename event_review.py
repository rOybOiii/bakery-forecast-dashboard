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


@dataclass(frozen=True)
class EventSplitEvidence:
    """Posterior evidence for keeping two peaks together or separating them."""

    left_peak_date: date
    right_peak_date: date
    same_event_probability: float
    separate_event_probability: float

    @property
    def recommends_split(self) -> bool:
        return self.separate_event_probability > self.same_event_probability


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


# The published demonstration bundle predates the ERP split-evidence contract.
# These fixtures let the UI exercise that contract without claiming that the
# values came from a live model run. Production bundles will carry these fields.
DEMO_SPLIT_EVIDENCE: dict[str, EventSplitEvidence] = {
    "downtown_0_019": EventSplitEvidence(
        left_peak_date=date(2025, 2, 14),
        right_peak_date=date(2025, 2, 18),
        same_event_probability=0.38,
        separate_event_probability=0.62,
    ),
    "downtown_0_013": EventSplitEvidence(
        left_peak_date=date(2024, 9, 26),
        right_peak_date=date(2024, 9, 30),
        same_event_probability=0.44,
        separate_event_probability=0.56,
    ),
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
    suggested_name = str(metadata.get("suggested_name", "Unidentified"))
    return {
        "name": "Unidentified",
        "suggested_name": suggested_name,
        "suggested_family": str(metadata.get("family", "Unassigned")),
        "suggested_category": str(metadata.get("category", "Unclassified")),
        "family": "Unassigned",
        "category": "Unclassified",
        "tags": (f"{direction}-lift",),
        "status": str(metadata.get("status", "Needs review")),
        "notes": "",
    }


def auto_phase_segments(
    start_date: date,
    end_date: date,
    *,
    peak_date: date | None = None,
) -> tuple[EventSegment, ...]:
    """Create automatic buildup, crest, single-peak, and tail phases."""

    duration = (end_date - start_date).days + 1
    if duration < 1:
        raise ValueError("Event end date must not precede its start date")
    peak = peak_date or start_date + timedelta(days=(duration - 1) // 2)
    if not start_date <= peak <= end_date:
        raise ValueError("Peak date must fall inside the event")

    phases: list[EventSegment] = []
    left_crest = peak - timedelta(days=1) if peak > start_date else None
    right_crest = peak + timedelta(days=1) if peak < end_date else None
    if left_crest and start_date <= left_crest - timedelta(days=1):
        phases.append(EventSegment("Buildup", start_date, left_crest - timedelta(days=1)))
    if left_crest:
        phases.append(EventSegment("Crest", left_crest, left_crest))
    phases.append(EventSegment("Peak", peak, peak))
    if right_crest:
        phases.append(EventSegment("Crest", right_crest, right_crest))
    if right_crest and right_crest + timedelta(days=1) <= end_date:
        phases.append(EventSegment("Tail", right_crest + timedelta(days=1), end_date))
    return tuple(phases)


def default_segments(start_date: date, end_date: date) -> tuple[EventSegment, ...]:
    """Backward-compatible alias for the automatic phase assignment."""

    return auto_phase_segments(start_date, end_date)


EVENT_PHASES = ("Buildup", "Crest", "Peak", "Tail")


def auto_phase_assignments(
    start_date: date,
    end_date: date,
    *,
    peak_date: date | None = None,
) -> tuple[tuple[date, str], ...]:
    """Expand the automatic morphology into an editable label for every day."""

    assignments: list[tuple[date, str]] = []
    for segment in auto_phase_segments(start_date, end_date, peak_date=peak_date):
        current = segment.start_date
        while current <= segment.end_date:
            assignments.append((current, segment.name))
            current += timedelta(days=1)
    return tuple(sorted(assignments))


def segments_from_phase_assignments(
    assignments: Iterable[tuple[date, str]],
) -> tuple[EventSegment, ...]:
    """Collapse editable daily phase labels into contiguous display segments."""

    ordered = sorted(assignments)
    if not ordered:
        return ()
    segments: list[EventSegment] = []
    segment_start, segment_name = ordered[0]
    previous_date = segment_start
    for current_date, current_name in ordered[1:]:
        if current_date != previous_date + timedelta(days=1) or current_name != segment_name:
            segments.append(EventSegment(segment_name, segment_start, previous_date))
            segment_start, segment_name = current_date, current_name
        previous_date = current_date
    segments.append(EventSegment(segment_name, segment_start, previous_date))
    return tuple(segments)


def validate_phase_assignments(
    assignments: Iterable[tuple[date, str]],
) -> tuple[str, ...]:
    """Require complete known phases and exactly one reviewed peak day."""

    rows = list(assignments)
    errors: list[str] = []
    if any(phase not in EVENT_PHASES for _, phase in rows):
        errors.append("Every day needs a recognized phase classification.")
    if sum(phase == "Peak" for _, phase in rows) != 1:
        errors.append("Choose exactly one peak day for this occurrence.")
    if len({day for day, _ in rows}) != len(rows):
        errors.append("Each event day can appear only once.")
    return tuple(errors)


def events_in_window(
    events,
    *,
    start_date: date,
    end_date: date,
):
    """Return occurrences overlapping the selected timeline."""

    if start_date > end_date:
        raise ValueError("Timeline start date must not follow its end date")
    return events.loc[
        events["end_date"].dt.date.ge(start_date)
        & events["start_date"].dt.date.le(end_date)
    ].copy()


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
