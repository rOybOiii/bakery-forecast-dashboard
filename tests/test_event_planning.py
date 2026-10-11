from datetime import date

import pandas as pd

from event_planning import (
    make_placement,
    recurring_starts,
    resample_effect,
    score_event_plan,
)


def baseline() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "target_date": pd.date_range("2026-10-11", periods=14, freq="D"),
            "operational_median": [10_000.0] * 14,
            "operational_q05": [9_000.0] * 14,
            "operational_q95": [11_000.0] * 14,
        }
    )


def test_shape_resampling_preserves_endpoints_and_requested_duration():
    stretched = resample_effect((0.08, 0.22, 0.14), 5)
    assert len(stretched) == 5
    assert stretched[0] == 0.08
    assert stretched[-1] == 0.14
    assert max(stretched) == 0.22


def test_positive_event_adds_lift_and_uncertainty_without_mutating_input():
    source = baseline()
    placement = make_placement(
        "payday-friday",
        start_date=date(2026, 10, 16),
    )
    result = score_event_plan(source, [placement])
    assert result.event_lift == 2_800.0
    assert result.placement_count == 1
    assert result.daily.loc[5, "event_operational_median"] == 11_800.0
    assert "event_lift" not in source.columns
    assert result.daily.loc[5, "event_operational_q05"] < result.daily.loc[5, "event_operational_q95"]


def test_closed_day_zeros_all_forecast_quantiles():
    placement = make_placement(
        "closed-day",
        start_date=date(2026, 10, 14),
    )
    result = score_event_plan(baseline(), [placement])
    closed = result.daily.loc[3]
    assert closed["event_operational_median"] == 0
    assert closed["event_operational_q05"] == 0
    assert closed["event_operational_q95"] == 0


def test_custom_daily_shape_is_used_for_forecast_copy_only():
    placement = make_placement(
        "downtown-arts-walk",
        start_date=date(2026, 10, 15),
        duration_days=4,
        custom_daily_percent=(6, 25, 17, 3),
    )
    assert placement.daily_effect == (0.06, 0.25, 0.17, 0.03)
    assert placement.peak_percent == 25


def test_recurrence_stays_inside_visible_horizon():
    starts = recurring_starts(
        date(2026, 10, 11),
        horizon_end=date(2026, 10, 24),
        every_weeks=1,
    )
    assert starts == (date(2026, 10, 11), date(2026, 10, 18))
