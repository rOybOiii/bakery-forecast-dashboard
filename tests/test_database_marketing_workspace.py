from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from database_marketing_workspace import (
    build_known_effect_experiment,
    build_spend_block_rows,
    normalize_historical_spend,
)


def test_historical_spend_is_optional_but_normalizes_when_provided() -> None:
    start = date.today() - timedelta(days=1)
    table = pd.DataFrame(
        {
            "Date": [start.isoformat(), date.today().isoformat()],
            "Search": [0, 25],
            "Social": [10, 0],
        }
    )

    rows = normalize_historical_spend(table, ["Search", "Social"])

    assert len(rows) == 4
    assert rows[0] == {
        "date": start.isoformat(),
        "channel": "Search",
        "amount": 0.0,
    }
    assert rows[-1]["channel"] == "Social"
    assert rows[-1]["amount"] == 0.0


def test_historical_spend_requires_every_configured_channel() -> None:
    table = pd.DataFrame({"Date": [date.today().isoformat()], "Search": [12]})

    with pytest.raises(ValueError, match="missing the Social column"):
        normalize_historical_spend(table, ["Search", "Social"])


def test_historical_spend_rejects_future_dates() -> None:
    table = pd.DataFrame(
        {
            "Date": [(date.today() + timedelta(days=1)).isoformat()],
            "Search": [12],
        }
    )

    with pytest.raises(ValueError, match="future dates"):
        normalize_historical_spend(table, ["Search"])


def test_block_plan_preserves_budget_and_inserts_zero_spend_pause() -> None:
    start = date.today() + timedelta(days=1)
    rows = build_spend_block_rows(
        start_date=start,
        channel_totals={1: 700.0, 2: 350.0},
        cadence_days=4,
        style="learning",
        pause_after={1: 2},
    )

    assert rows[0]["start_date"] == start
    assert rows[-1]["end_date"] == start + timedelta(days=13)
    assert all(
        left["end_date"] + timedelta(days=1) == right["start_date"]
        for left, right in zip(rows, rows[1:])
    )
    pause = next(row for row in rows if row["pause"])
    assert set(pause["daily_spend"].values()) == {0.0}
    totals = {
        channel_id: round(
            sum(
                row["daily_spend"][channel_id]
                * ((row["end_date"] - row["start_date"]).days + 1)
                for row in rows
            ),
            2,
        )
        for channel_id in (1, 2)
    }
    assert totals[1] == pytest.approx(700.0, abs=0.15)
    assert totals[2] == pytest.approx(350.0, abs=0.15)


def test_learning_blocks_create_spend_variation() -> None:
    rows = build_spend_block_rows(
        start_date=date.today(),
        channel_totals={1: 500.0},
        cadence_days=3,
        style="learning",
    )

    active_values = {
        row["daily_spend"][1] for row in rows if not row["pause"]
    }
    assert len(active_values) >= 3


def test_known_effect_experiment_pairs_fixed_spend_with_sales_lift() -> None:
    start = date(2026, 6, 1)
    observations = pd.DataFrame(
        {
            "date": [start - timedelta(days=2)]
            + [start + timedelta(days=offset) for offset in range(50)],
            "value": [900.0] + [1000.0 + offset for offset in range(50)],
        }
    )
    channels = [
        {"channel_id": 2, "name": "Search", "enabled": True},
        {"channel_id": 3, "name": "Social", "enabled": True},
    ]

    experiment = build_known_effect_experiment(observations, channels)

    assert experiment["start_date"] == start
    assert experiment["end_date"] == start + timedelta(days=41)
    assert len(experiment["plans"]) == 3
    assert all(len(plan["spend_blocks"]) == 14 for plan in experiment["plans"])
    assert all(value > 0 for value in experiment["known_lift"])
    assert list(experiment["sales"]["Sales"]) == pytest.approx(
        [1000.0 + offset + experiment["known_lift"][offset] for offset in range(42)]
    )
    first_plan = experiment["plans"][0]["spend_blocks"]
    assert len({block["daily_spend"][2] for block in first_plan}) >= 8
    assert [block["daily_spend"][2] for block in first_plan] != [
        block["daily_spend"][3] for block in first_plan
    ]
