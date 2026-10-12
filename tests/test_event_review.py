from datetime import date

import pandas as pd

from event_review import (
    EventSegment,
    auto_phase_segments,
    default_annotation,
    events_in_window,
    family_summary,
    segments_from_phase_assignments,
    validate_phase_assignments,
    validate_segments,
)


def test_default_segments_cover_long_event_without_overlap():
    segments = auto_phase_segments(
        date(2026, 7, 3), date(2026, 7, 7), peak_date=date(2026, 7, 5)
    )

    assert [segment.name for segment in segments] == [
        "Buildup",
        "Crest",
        "Peak",
        "Crest",
        "Tail",
    ]
    assert segments[0].start_date == date(2026, 7, 3)
    assert segments[-1].end_date == date(2026, 7, 7)
    assert [segment.duration_days for segment in segments if segment.name == "Peak"] == [1]
    assert not validate_segments(
        segments,
        event_start=date(2026, 7, 3),
        event_end=date(2026, 7, 7),
    )


def test_segment_validation_rejects_overlap_and_out_of_bounds():
    segments = (
        EventSegment("Buildup", date(2026, 7, 2), date(2026, 7, 4)),
        EventSegment("Peak", date(2026, 7, 4), date(2026, 7, 5)),
    )

    errors = validate_segments(
        segments,
        event_start=date(2026, 7, 3),
        event_end=date(2026, 7, 5),
    )

    assert any("outside" in error for error in errors)
    assert any("overlap" in error for error in errors)


def test_family_summary_keeps_current_and_historical_occurrences():
    summary = family_summary(
        "Payday weekend",
        ({"peak_percent": 10.0, "duration_days": 5},),
    )

    assert summary["occurrence_count"] == 3
    assert summary["typical_peak_percent"] == 8.4
    assert summary["typical_duration_days"] == 3


def test_unreviewed_detection_is_unidentified_and_unassigned():
    annotation = default_annotation("downtown_0_020", direction="positive")

    assert annotation["name"] == "Unidentified"
    assert annotation["family"] == "Unassigned"
    assert annotation["suggested_name"] == "Payday weekend"


def test_event_occurrences_are_scoped_to_overlapping_timeline():
    events = pd.DataFrame(
        {
            "disturbance_id": ["before", "overlap", "inside", "after"],
            "start_date": pd.to_datetime(
                ["2025-01-01", "2025-01-08", "2025-01-12", "2025-02-01"]
            ),
            "end_date": pd.to_datetime(
                ["2025-01-04", "2025-01-11", "2025-01-14", "2025-02-03"]
            ),
        }
    )

    visible = events_in_window(
        events,
        start_date=date(2025, 1, 10),
        end_date=date(2025, 1, 20),
    )

    assert visible["disturbance_id"].tolist() == ["overlap", "inside"]


def test_reviewed_phases_can_end_in_crest_without_a_tail():
    assignments = (
        (date(2026, 7, 3), "Peak"),
        (date(2026, 7, 4), "Crest"),
        (date(2026, 7, 5), "Crest"),
    )

    segments = segments_from_phase_assignments(assignments)

    assert not validate_phase_assignments(assignments)
    assert [segment.name for segment in segments] == ["Peak", "Crest"]
    assert segments[-1].end_date == date(2026, 7, 5)


def test_reviewed_phases_require_one_peak_day():
    assignments = (
        (date(2026, 7, 3), "Crest"),
        (date(2026, 7, 4), "Crest"),
    )

    assert validate_phase_assignments(assignments) == (
        "Choose exactly one peak day for this occurrence.",
    )
