from datetime import date

import pandas as pd

from event_planning import (
    event_source_effect,
    family_analysis_by_template,
    make_placement,
    recurring_starts,
    replace_placement,
    resample_effect,
    score_event_plan,
    template_by_id,
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


def test_replacing_an_edited_shape_preserves_the_timeline_slot():
    original = make_placement(
        "payday-friday",
        start_date=date(2026, 10, 11),
        placement_id="selected-event",
    )
    later = make_placement(
        "payday-friday",
        start_date=date(2026, 10, 18),
        placement_id="later-event",
    )
    edited = make_placement(
        "payday-friday",
        start_date=date(2026, 10, 11),
        custom_daily_percent=(18, 30),
        placement_id="selected-event",
    )

    updated = replace_placement((original, later), edited)

    assert [placement.placement_id for placement in updated] == [
        "selected-event",
        "later-event",
    ]
    assert updated[0].daily_effect == (0.18, 0.30)
    assert updated[1] == later


def test_family_estimate_and_prior_occurrence_are_distinct_starting_shapes():
    family = event_source_effect(
        "downtown-arts-walk",
        source="Family estimate",
        duration_days=3,
    )
    prior = event_source_effect(
        "downtown-arts-walk",
        source="Prior occurrence",
        duration_days=3,
        occurrence_label="2023",
    )

    assert family == (0.10, 0.22, 0.14)
    assert prior == (0.08, 0.18, 0.12)
    assert max(family) > max(prior)


def test_blank_event_source_preserves_requested_duration():
    blank = event_source_effect(
        "payday-friday",
        source="Start blank",
        duration_days=5,
    )

    assert blank == (0.0,) * 5


def test_family_analysis_exposes_reviewed_history_and_corrections():
    analysis = family_analysis_by_template("payday-friday")

    assert analysis is not None
    assert len(analysis.occurrences) == 4
    assert analysis.corrections
    assert analysis.direction == "Stable"


def test_backend_materialized_family_is_available_for_immediate_scenario_use():
    template = template_by_id("erp-positive-pulse-demo")
    analysis = family_analysis_by_template("erp-positive-pulse-demo")

    assert template.peak_percent == 18
    assert analysis is not None
    assert len(analysis.occurrences) == 5
    assert "ERP" in analysis.evidence_quality
