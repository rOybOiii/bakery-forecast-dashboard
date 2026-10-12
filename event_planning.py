"""Static event catalog and session-only event scenario scoring for the MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from uuid import uuid4

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class EventTemplate:
    template_id: str
    name: str
    category: str
    tags: tuple[str, ...]
    evidence: str
    timing_hint: str
    daily_effect: tuple[float, ...]
    uncertainty_low_factor: float = 0.55
    uncertainty_high_factor: float = 1.45
    recommended: bool = False

    @property
    def default_duration(self) -> int:
        return len(self.daily_effect)

    @property
    def peak_percent(self) -> int:
        return int(round(100 * max(self.daily_effect, key=abs)))


@dataclass(frozen=True)
class EventPlacement:
    placement_id: str
    template_id: str
    name: str
    category: str
    tags: tuple[str, ...]
    start_date: date
    daily_effect: tuple[float, ...]
    daily_low: tuple[float, ...]
    daily_high: tuple[float, ...]
    evidence: str
    occurrence: str = "One time"

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=len(self.daily_effect) - 1)

    @property
    def peak_percent(self) -> int:
        return int(round(100 * max(self.daily_effect, key=abs)))


@dataclass(frozen=True)
class EventScenarioResult:
    daily: pd.DataFrame
    event_lift: float
    placement_count: int


@dataclass(frozen=True)
class FamilyOccurrence:
    label: str
    daily_effect: tuple[float, ...]
    context: str


@dataclass(frozen=True)
class EventFamilyAnalysis:
    template_id: str
    family_name: str
    occurrences: tuple[FamilyOccurrence, ...]
    direction: str
    evidence_quality: str
    typical_peak_percent: float
    corrections: tuple[tuple[str, float], ...]


EVENT_CATALOG: tuple[EventTemplate, ...] = (
    EventTemplate(
        template_id="payday-friday",
        name="Payday Friday",
        category="Calendar",
        tags=("payday", "recurring", "two-day", "sharp-peak"),
        evidence="6 prior occurrences · restaurant-specific",
        timing_hint="Expected during this forecast window",
        daily_effect=(0.18, 0.10),
        recommended=True,
    ),
    EventTemplate(
        template_id="downtown-arts-walk",
        name="Downtown Arts Walk",
        category="Local/community",
        tags=("annual", "downtown", "three-day", "slow-decay"),
        evidence="3 years · trend increasing",
        timing_hint="Occurred during the same week last year",
        daily_effect=(0.10, 0.22, 0.14),
        recommended=True,
    ),
    EventTemplate(
        template_id="fall-break-weekend",
        name="Fall break weekend",
        category="School/sports",
        tags=("annual", "weekend", "three-day", "wide-uncertainty"),
        evidence="4 comparable events · category estimate",
        timing_hint="Likely during this forecast window",
        daily_effect=(0.08, 0.15, 0.12),
        uncertainty_low_factor=0.35,
        uncertainty_high_factor=1.65,
        recommended=True,
    ),
    EventTemplate(
        template_id="football-home-game",
        name="Football home game",
        category="School/sports",
        tags=("downtown", "one-day", "sharp-peak", "sports"),
        evidence="8 prior occurrences · variable timing",
        timing_hint="Searchable catalog event",
        daily_effect=(0.16,),
    ),
    EventTemplate(
        template_id="farmers-market-opening",
        name="Farmers Market opening",
        category="Local/community",
        tags=("annual", "downtown", "two-day", "stable"),
        evidence="2 years · stable effect",
        timing_hint="Searchable catalog event",
        daily_effect=(0.13, 0.08),
    ),
    EventTemplate(
        template_id="holiday-rush",
        name="Holiday rush",
        category="Holiday",
        tags=("holiday", "four-day", "ramp", "high-volume"),
        evidence="5 comparable holiday periods",
        timing_hint="Searchable catalog event",
        daily_effect=(0.08, 0.14, 0.23, 0.15),
    ),
    EventTemplate(
        template_id="promotion-bump",
        name="In-store promotion",
        category="Promotion",
        tags=("promotion", "three-day", "sharp-peak", "negative-tail"),
        evidence="9 comparable promotions",
        timing_hint="Searchable catalog event",
        daily_effect=(0.11, 0.19, -0.04),
    ),
    EventTemplate(
        template_id="closed-day",
        name="Closed day",
        category="Operational",
        tags=("closure", "one-day", "operations"),
        evidence="Operating schedule · deterministic",
        timing_hint="Operational exception",
        daily_effect=(-1.0,),
        uncertainty_low_factor=1.0,
        uncertainty_high_factor=1.0,
        recommended=True,
    ),
)


FAMILY_ANALYSES: dict[str, EventFamilyAnalysis] = {
    "payday-friday": EventFamilyAnalysis(
        template_id="payday-friday",
        family_name="Payday Friday",
        occurrences=(
            FamilyOccurrence("Apr 2026", (0.17, 0.09), "Friday payday"),
            FamilyOccurrence("Mar 2026", (0.19, 0.11), "Friday payday"),
            FamilyOccurrence("Feb 2026", (0.16, 0.10), "Friday payday"),
            FamilyOccurrence("Jan 2026", (0.18, 0.09), "Friday payday"),
        ),
        direction="Stable",
        evidence_quality="Good family evidence",
        typical_peak_percent=16.5,
        corrections=(("Recent family direction", 0.8), ("Downtown response", 0.7)),
    ),
    "downtown-arts-walk": EventFamilyAnalysis(
        template_id="downtown-arts-walk",
        family_name="Downtown Arts Walk",
        occurrences=(
            FamilyOccurrence("2025", (0.10, 0.21, 0.14), "Saturday peak"),
            FamilyOccurrence("2024", (0.09, 0.20, 0.13), "Saturday peak"),
            FamilyOccurrence("2023", (0.08, 0.18, 0.12), "Friday opening"),
        ),
        direction="Strengthening slightly",
        evidence_quality="Moderate family evidence",
        typical_peak_percent=20.0,
        corrections=(("Recent family direction", 1.4), ("Downtown response", 0.6)),
    ),
    "fall-break-weekend": EventFamilyAnalysis(
        template_id="fall-break-weekend",
        family_name="Fall break weekend",
        occurrences=(
            FamilyOccurrence("2025", (0.07, 0.14, 0.11), "Weekend"),
            FamilyOccurrence("2024", (0.09, 0.16, 0.13), "Weekend"),
            FamilyOccurrence("Comparable break A", (0.06, 0.13, 0.10), "Category match"),
            FamilyOccurrence("Comparable break B", (0.08, 0.14, 0.12), "Category match"),
        ),
        direction="No reliable trend",
        evidence_quality="Category-supported estimate",
        typical_peak_percent=14.0,
        corrections=(("Calendar alignment", 0.5), ("Downtown response", 0.5)),
    ),
    "football-home-game": EventFamilyAnalysis(
        template_id="football-home-game",
        family_name="Football home game",
        occurrences=(
            FamilyOccurrence("Game 8", (0.18,), "Evening kickoff"),
            FamilyOccurrence("Game 7", (0.14,), "Afternoon kickoff"),
            FamilyOccurrence("Game 6", (0.16,), "Evening kickoff"),
            FamilyOccurrence("Game 5", (0.15,), "Afternoon kickoff"),
        ),
        direction="Variable by kickoff",
        evidence_quality="Good family evidence",
        typical_peak_percent=15.5,
        corrections=(("Expected kickoff context", 0.3), ("Downtown response", 0.2)),
    ),
    "farmers-market-opening": EventFamilyAnalysis(
        template_id="farmers-market-opening",
        family_name="Farmers Market opening",
        occurrences=(
            FamilyOccurrence("2025", (0.14, 0.08), "Opening weekend"),
            FamilyOccurrence("2024", (0.12, 0.08), "Opening weekend"),
        ),
        direction="Stable",
        evidence_quality="Limited family evidence",
        typical_peak_percent=12.0,
        corrections=(("Recent family direction", 0.5), ("Downtown response", 0.5)),
    ),
    "holiday-rush": EventFamilyAnalysis(
        template_id="holiday-rush",
        family_name="Holiday rush",
        occurrences=(
            FamilyOccurrence("2025 holiday", (0.07, 0.13, 0.22, 0.14), "Four-day rush"),
            FamilyOccurrence("2024 holiday", (0.08, 0.15, 0.24, 0.16), "Four-day rush"),
            FamilyOccurrence("Comparable holiday A", (0.06, 0.12, 0.20, 0.14), "Category match"),
        ),
        direction="Stable with a sharp crest",
        evidence_quality="Moderate family evidence",
        typical_peak_percent=22.0,
        corrections=(("Seasonal direction", 0.5), ("Downtown response", 0.5)),
    ),
    "promotion-bump": EventFamilyAnalysis(
        template_id="promotion-bump",
        family_name="In-store promotion",
        occurrences=(
            FamilyOccurrence("Promotion 9", (0.10, 0.18, -0.03), "Weekend"),
            FamilyOccurrence("Promotion 8", (0.12, 0.20, -0.05), "Weekday"),
            FamilyOccurrence("Promotion 7", (0.09, 0.17, -0.04), "Weekend"),
        ),
        direction="Stable peak · variable tail",
        evidence_quality="Good family evidence",
        typical_peak_percent=18.0,
        corrections=(("Recent family direction", 0.4), ("Downtown response", 0.6)),
    ),
}


def template_by_id(template_id: str) -> EventTemplate:
    for template in EVENT_CATALOG:
        if template.template_id == template_id:
            return template
    raise KeyError(f"Unknown event template: {template_id}")


def family_analysis_by_template(template_id: str) -> EventFamilyAnalysis | None:
    return FAMILY_ANALYSES.get(template_id)


def event_source_effect(
    template_id: str,
    *,
    source: str,
    duration_days: int,
    occurrence_label: str | None = None,
) -> tuple[float, ...]:
    """Return the selected scenario starting shape without altering history."""

    template = template_by_id(template_id)
    if source == "Family estimate":
        return resample_effect(template.daily_effect, duration_days)
    if source == "Start blank":
        return tuple(0.0 for _ in range(duration_days))
    if source != "Prior occurrence":
        raise ValueError(f"Unknown event shape source: {source}")
    analysis = family_analysis_by_template(template_id)
    if analysis is None or not analysis.occurrences:
        raise ValueError("This event does not have prior family occurrences")
    selected = next(
        (
            occurrence
            for occurrence in analysis.occurrences
            if occurrence.label == occurrence_label
        ),
        analysis.occurrences[0],
    )
    return resample_effect(selected.daily_effect, duration_days)


def resample_effect(effect: tuple[float, ...], duration_days: int) -> tuple[float, ...]:
    """Stretch an event shape across a new duration without changing its character."""

    if duration_days < 1:
        raise ValueError("Event duration must be at least one day")
    if len(effect) == duration_days:
        return effect
    if len(effect) == 1:
        return tuple(effect[0] for _ in range(duration_days))
    source = np.linspace(0.0, 1.0, len(effect))
    target = np.linspace(0.0, 1.0, duration_days)
    return tuple(float(value) for value in np.interp(target, source, effect))


def make_placement(
    template_id: str,
    *,
    start_date: date,
    duration_days: int | None = None,
    magnitude_percent: int = 100,
    custom_daily_percent: tuple[float, ...] | None = None,
    occurrence: str = "One time",
    placement_id: str | None = None,
) -> EventPlacement:
    template = template_by_id(template_id)
    duration = duration_days or template.default_duration
    if custom_daily_percent is None:
        scale = magnitude_percent / 100.0
        effect = tuple(value * scale for value in resample_effect(template.daily_effect, duration))
    else:
        if len(custom_daily_percent) != duration:
            raise ValueError("Custom daily shape must match the event duration")
        effect = tuple(float(value) / 100.0 for value in custom_daily_percent)

    low: list[float] = []
    high: list[float] = []
    for value in effect:
        if value >= 0:
            low.append(value * template.uncertainty_low_factor)
            high.append(value * template.uncertainty_high_factor)
        else:
            low.append(value * template.uncertainty_high_factor)
            high.append(value * template.uncertainty_low_factor)
    return EventPlacement(
        placement_id=placement_id or str(uuid4()),
        template_id=template.template_id,
        name=template.name,
        category=template.category,
        tags=template.tags,
        start_date=start_date,
        daily_effect=effect,
        daily_low=tuple(low),
        daily_high=tuple(high),
        evidence=template.evidence,
        occurrence=occurrence,
    )


def recurring_starts(
    start_date: date,
    *,
    horizon_end: date,
    every_weeks: int,
    maximum_occurrences: int = 12,
) -> tuple[date, ...]:
    if every_weeks < 1:
        raise ValueError("Recurrence must be at least one week")
    starts: list[date] = []
    current = start_date
    while current <= horizon_end and len(starts) < maximum_occurrences:
        starts.append(current)
        current += timedelta(days=7 * every_weeks)
    return tuple(starts)


def score_event_plan(
    daily: pd.DataFrame, placements: tuple[EventPlacement, ...] | list[EventPlacement]
) -> EventScenarioResult:
    """Layer planned event effects onto a Forecast 2 plus MMM daily frame."""

    frame = daily.copy()
    date_values = pd.to_datetime(frame["target_date"]).dt.date
    median_column = "operational_median" if "operational_median" in frame else "median"
    q05_column = "operational_q05" if "operational_q05" in frame else "q05"
    q95_column = "operational_q95" if "operational_q95" in frame else "q95"
    frame["event_lift"] = 0.0
    frame["event_q05_lift"] = 0.0
    frame["event_q95_lift"] = 0.0

    for placement in placements:
        for offset, (median_effect, low_effect, high_effect) in enumerate(
            zip(placement.daily_effect, placement.daily_low, placement.daily_high)
        ):
            target = placement.start_date + timedelta(days=offset)
            mask = date_values.eq(target)
            if not mask.any():
                continue
            frame.loc[mask, "event_lift"] += frame.loc[mask, median_column] * median_effect
            frame.loc[mask, "event_q05_lift"] += frame.loc[mask, q05_column] * low_effect
            frame.loc[mask, "event_q95_lift"] += frame.loc[mask, q95_column] * high_effect

    frame["event_operational_median"] = (
        frame[median_column] + frame["event_lift"]
    ).clip(lower=0.0)
    frame["event_operational_q05"] = (
        frame[q05_column] + frame["event_q05_lift"]
    ).clip(lower=0.0)
    frame["event_operational_q95"] = (
        frame[q95_column] + frame["event_q95_lift"]
    ).clip(lower=0.0)
    return EventScenarioResult(
        daily=frame,
        event_lift=float(frame["event_lift"].sum()),
        placement_count=len(placements),
    )


def placements_equivalent(
    left: tuple[EventPlacement, ...] | list[EventPlacement],
    right: tuple[EventPlacement, ...] | list[EventPlacement],
) -> bool:
    return tuple(left) == tuple(right)


def replace_placement(
    placements: tuple[EventPlacement, ...] | list[EventPlacement],
    replacement: EventPlacement,
) -> tuple[EventPlacement, ...]:
    """Replace one placed scenario copy while preserving timeline order."""

    if not any(
        placement.placement_id == replacement.placement_id
        for placement in placements
    ):
        raise KeyError(f"Unknown event placement: {replacement.placement_id}")
    return tuple(
        replacement
        if placement.placement_id == replacement.placement_id
        else placement
        for placement in placements
    )
