from datetime import date

from database_events_workspace import (
    _clean_tags,
    _event_label,
    _reconciliation_label,
    _reconciliation_status_for_event,
)


def test_clean_tags_normalizes_and_deduplicates() -> None:
    assert _clean_tags(" Weekend, positive lift, weekend,  ") == [
        "weekend",
        "positive-lift",
    ]


def test_event_label_uses_manager_title_when_present() -> None:
    assert _event_label(
        {
            "start_date": date(2026, 9, 14),
            "end_date": date(2026, 9, 16),
            "title": "Downtown festival",
        }
    ) == "Sep 14–Sep 16, 2026 · Downtown festival"


def test_reconciliation_label_explains_old_event_and_relationship() -> None:
    assert _reconciliation_label(
        {
            "source_start_date": date(2026, 9, 14),
            "source_end_date": date(2026, 9, 16),
            "source_title": "Downtown festival",
            "relationship": "split",
        }
    ) == "Sep 14–Sep 16, 2026 · Downtown festival · Split"


def test_pending_reconciliation_takes_priority_on_event_notice() -> None:
    chosen = _reconciliation_status_for_event(
        88,
        [
            {"target_event_id": 88, "decision": "auto_accepted", "id": 1},
            {"target_event_id": 88, "decision": "pending", "id": 2},
            {"target_event_id": 99, "decision": "pending", "id": 3},
        ],
    )

    assert chosen is not None
    assert chosen["id"] == 2
