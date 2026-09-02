from pathlib import Path

from dashboard_data import (
    disturbance_rows,
    forecast_rows,
    history_rows,
    load_bundle,
    recent_rows,
    restaurant_summary,
)


BUNDLE = Path(__file__).resolve().parents[1] / "data" / "dashboard_bundle_v1"


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
