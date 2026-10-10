from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from marketing_planning import (
    CHANNEL_ORDER,
    MarketingPlanState,
    empty_state,
    implement_plan,
    initialize_draft,
    load_plan_state,
    make_plan,
    optimize_allocations,
    save_draft,
    save_plan_state,
    score_plan,
)


def baseline() -> pd.DataFrame:
    dates = pd.date_range("2026-10-11", periods=14, freq="D")
    return pd.DataFrame(
        {
            "target_date": dates,
            "median": [9000 + (index % 7) * 250 for index in range(14)],
            "q05": [8200 + (index % 7) * 250 for index in range(14)],
            "q95": [9800 + (index % 7) * 250 for index in range(14)],
        }
    )


def test_draft_initialization_ladder_prefers_saved_then_published_then_zero():
    start = date(2026, 10, 11)
    zero = initialize_draft(
        empty_state(),
        restaurant="downtown",
        start_date=start,
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    assert zero.total_budget == 0
    assert all(zero.channels[key].eligible for key in CHANNEL_ORDER)

    published = make_plan(
        restaurant="downtown",
        start_date=start,
        end_date=start + timedelta(days=13),
        total_budget=4200,
        eligible={key: True for key in CHANNEL_ORDER},
        allocation_mode="optimized",
        allocations=None,
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    state = MarketingPlanState(1, published, None)
    from_published = initialize_draft(
        state,
        restaurant="downtown",
        start_date=start + timedelta(days=1),
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    assert from_published.total_budget == 4200
    assert from_published.plan_id != published.plan_id

    saved = make_plan(
        restaurant="downtown",
        start_date=start,
        end_date=start + timedelta(days=6),
        total_budget=2500,
        eligible={"search": True, "social": True, "radio": False},
        allocation_mode="optimized",
        allocations=None,
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    chosen = initialize_draft(
        MarketingPlanState(1, published, saved),
        restaurant="downtown",
        start_date=start,
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    assert chosen.plan_id == saved.plan_id


def test_optimizer_respects_eligibility_and_reconciles_total():
    allocation = optimize_allocations(4321, ["search", "radio"], 14)
    assert allocation["social"] == 0
    assert abs(sum(allocation.values()) - 4321) < 0.01
    assert allocation["search"] > 0
    assert allocation["radio"] > 0


def test_scorer_returns_marketing_lift_uncertainty_and_carryover():
    plan = make_plan(
        restaurant="downtown",
        start_date=date(2026, 10, 11),
        end_date=date(2026, 10, 24),
        total_budget=4200,
        eligible={key: True for key in CHANNEL_ORDER},
        allocation_mode="optimized",
        allocations=None,
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    result = score_plan(baseline(), plan)
    assert len(result.daily) == 14
    assert result.summary.marketing_lift > 0
    assert result.summary.expected_total > result.summary.organic_total
    assert result.summary.carryover_beyond_horizon > 0
    assert (result.daily["operational_q05"] < result.daily["operational_q95"]).all()
    assert set(result.saturation) == set(CHANNEL_ORDER)


def test_document_save_draft_then_implement_is_atomic_and_reloads(tmp_path: Path):
    path = tmp_path / "plan-state.json"
    plan = make_plan(
        restaurant="downtown",
        start_date=date(2026, 10, 11),
        end_date=date(2026, 10, 17),
        total_budget=1800,
        eligible={key: True for key in CHANNEL_ORDER},
        allocation_mode="optimized",
        allocations=None,
        baseline_vintage="forecast-2-demo",
        mmm_fit_version="mmm-demo-v1",
    )
    drafted = save_draft(empty_state(), plan)
    save_plan_state(path, drafted)
    loaded = load_plan_state(path)
    assert loaded.draft_plan is not None
    assert loaded.published_plan is None

    implemented = implement_plan(loaded, loaded.draft_plan)
    save_plan_state(path, implemented)
    reloaded = load_plan_state(path)
    assert reloaded.draft_plan is None
    assert reloaded.published_plan is not None
    assert reloaded.published_plan.total_budget == 1800
