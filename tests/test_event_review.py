from datetime import date

from event_review import EventSegment, default_segments, family_summary, validate_segments


def test_default_segments_cover_long_event_without_overlap():
    segments = default_segments(date(2026, 7, 3), date(2026, 7, 7))

    assert [segment.name for segment in segments] == ["Buildup", "Peak", "Tail"]
    assert segments[0].start_date == date(2026, 7, 3)
    assert segments[-1].end_date == date(2026, 7, 7)
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
