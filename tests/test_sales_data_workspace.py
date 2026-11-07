from __future__ import annotations

import pandas as pd

from sales_data_workspace import _filter_sales_history, _recovery_stage


def _history(days: int = 400) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=days, freq="D"),
            "value": range(days),
        }
    )


def test_recent_history_ends_on_latest_accepted_sales_date() -> None:
    visible = _filter_sales_history(_history(), history_window="30 days")

    assert len(visible) == 30
    assert visible["date"].min() == pd.Timestamp("2026-01-06")
    assert visible["date"].max() == pd.Timestamp("2026-02-04")


def test_custom_history_is_inclusive_and_can_be_reversed() -> None:
    visible = _filter_sales_history(
        _history(),
        history_window="Custom",
        custom_range=("2025-02-10", "2025-02-01"),
    )

    assert len(visible) == 10
    assert visible["date"].min() == pd.Timestamp("2025-02-01")
    assert visible["date"].max() == pd.Timestamp("2025-02-10")


def test_recovery_with_pending_event_matches_needs_confirmation() -> None:
    stage, tone = _recovery_stage(
        {
            "status": "complete",
            "reconciliation_status": "complete",
            "pending_event_matches": 2,
        }
    )

    assert stage == "Forecast recovered · event confirmation needed"
    assert tone == "warning"
