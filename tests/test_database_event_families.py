from datetime import date

from database_event_families import _event_label, _family_shape_chart


def test_event_label_shows_date_direction_and_current_family() -> None:
    label = _event_label(
        {
            "start_date": date(2026, 9, 14),
            "end_date": date(2026, 9, 14),
            "direction": "positive",
            "family_name": "Payday weekend",
        }
    )

    assert label == "Sep 14, 2026 · Positive · Payday weekend"


def test_family_shape_chart_accepts_database_numeric_values() -> None:
    chart = _family_shape_chart(
        [
            {"day": 0, "low": "1.5", "median": "2.0", "high": "2.5"},
            {"day": 1, "low": "0.5", "median": "1.0", "high": "1.5"},
        ]
    )

    spec = chart.to_dict()
    assert len(spec["layer"]) == 2
    assert len(spec["datasets"][next(iter(spec["datasets"]))]) == 2
