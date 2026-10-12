from pathlib import Path

import pandas as pd

from dashboard_charts import forecast_replay_chart
from dashboard_data import (
    disturbance_rows,
    forecast_replay_origins,
    forecast_replay_rows,
    forecast_rows,
    history_rows,
    load_bundle,
    recent_rows,
    restaurant_summary,
)


BUNDLE = Path(__file__).resolve().parents[1] / "data" / "dashboard_bundle_v1"
SERVING = Path(__file__).resolve().parents[1] / "data" / "backend_snapshot_v2" / "snapshot.json"


def test_public_bundle_loads_and_is_complete():
    bundle = load_bundle(BUNDLE)
    assert bundle.validation["status"] == "valid"
    assert len(forecast_rows(bundle, "downtown")) == 14
    aggregate = forecast_rows(bundle, "all")
    assert len(aggregate) == 14
    assert aggregate[["q025", "q05", "q25", "q75", "q95", "q975"]].notna().all().all()
    assert len(recent_rows(bundle, "downtown")) == 14
    history = history_rows(bundle, "downtown")
    assert len(history) > 300
    assert history["baseline_median"].notna().all()
    assert restaurant_summary(bundle, "all")["forecast_14_day_total"] > 0


def test_unscaled_backend_forecast_is_registered_but_cannot_replace_dollars():
    bundle = load_bundle(BUNDLE, serving_snapshot_path=SERVING)
    downtown = forecast_rows(bundle, "downtown")

    assert bundle.manifest["serving_snapshot"]["contract_version"] == (
        "credent_backend_snapshot_v2"
    )
    assert bundle.manifest["serving_snapshot"]["model_run_id"] == (
        "closed-loop-week16-serving-v2"
    )
    assert not downtown["run_id"].eq("closed-loop-week16-serving-v2").any()
    assert bundle.manifest["serving_snapshot"]["restaurant_scales"] == {}


def test_disturbance_copy_is_public_safe_and_manager_ready():
    bundle = load_bundle(BUNDLE)
    rows = disturbance_rows(bundle, "all")
    assert len(rows) == 25
    assert rows["restaurant_label"].notna().all()
    assert rows["evidence_label"].notna().all()
    assert rows["evidence_explanation"].notna().all()
    assert rows["duration_label"].notna().all()
    assert rows["impact_label"].str.contains("daily impact").all()
    assert "source_runs" not in bundle.manifest


def test_each_restaurant_has_a_regime_for_the_dedicated_view():
    bundle = load_bundle(BUNDLE)
    for restaurant in ("downtown", "fscoffee", "hurricane", "springdale"):
        rows = disturbance_rows(bundle, restaurant)
        regimes = rows.loc[rows["disturbance_type"].eq("regime")]
        assert len(regimes) >= 1
        assert regimes["impact_label"].str.contains("daily impact").all()
        assert regimes["duration_label"].notna().all()


def test_restaurant_forecast_replay_uses_saved_vintages():
    bundle = load_bundle(BUNDLE)
    origins = forecast_replay_origins(bundle, "downtown")
    assert len(origins) == 91
    assert origins[0] == pd.Timestamp("2024-11-30")
    assert origins[-1] == pd.Timestamp("2025-02-28")

    evaluated = forecast_replay_rows(bundle, "downtown", origins[0])
    current = forecast_replay_rows(bundle, "downtown", origins[-1])
    assert len(evaluated) == 14
    assert evaluated["actual_available"].sum() >= 10
    assert evaluated["target_date"].gt(origins[0]).all()
    assert evaluated["vintage_kind"].eq("cutoff_backfill_demo").all()
    assert len(current) == 14
    assert not current["actual_available"].any()
    assert current["vintage_kind"].eq("archived_forecast_2").all()


def test_replay_readouts_follow_the_selected_14_day_vintage():
    bundle = load_bundle(BUNDLE)
    replay = bundle.replay.loc[bundle.replay["restaurant"].eq("downtown")]
    disturbances = bundle.disturbances.loc[
        bundle.disturbances["restaurant"].eq("downtown")
    ]
    figure = forecast_replay_chart(
        replay,
        disturbances,
        history_rows(bundle, "downtown"),
    )

    initial_miss = figure.data[0].text[1]
    future_miss = figure.frames[-1].data[0].text[1]
    assert "12 of 14 outcomes reported" in initial_miss
    assert "$539" in initial_miss
    assert "0 of 14 outcomes reported" in future_miss
    assert "Waiting" in future_miss
    assert list(figure.layout.xaxis.range) == [-0.8, 14.8]
    assert list(figure.layout.xaxis.tickvals) == [0, 7, 14]
    assert list(figure.layout.xaxis.ticktext) == [
        "Issue date",
        "+7 days",
        "+14 days",
    ]
    assert min(figure.data[5].x) == 0
    assert min(figure.data[8].x) == 1

    event_frames = [
        replay_frame
        for replay_frame in figure.frames
        if max(replay_frame.data[4].marker.size) > 1
    ]
    assert event_frames
    for replay_frame in event_frames:
        counterfactual = replay_frame.data[3]
        actual = replay_frame.data[5]
        assert min(counterfactual.x) >= 0
        assert len(counterfactual.x) == len(actual.x)
        assert any(
            expected_without_event != observed
            for expected_without_event, observed in zip(
                counterfactual.y, actual.y
            )
        )
