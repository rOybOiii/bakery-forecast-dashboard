import json
from pathlib import Path

import pytest

from serving_repository import load_serving_repository


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "backend_snapshot_v1" / "snapshot.json"


def test_published_snapshot_serves_forecast_and_family_without_model_imports():
    repository = load_serving_repository(SNAPSHOT)

    forecast = repository.forecast_frame("downtown")
    families = repository.event_family_records("downtown")

    assert repository.model_run_id == "closed-loop-week16-serving-v1"
    assert len(forecast) == 14
    assert forecast["unit"].eq("standardized_sales").all()
    assert forecast["serving_contract"].eq("credent_backend_snapshot_v1").all()
    assert len(families) == 1
    assert len(families[0]["occurrences"]) == 5
    assert max(
        point["median"] for point in families[0]["estimate"]["points"]
    ) == pytest.approx(0.18)


def test_repository_fails_closed_when_snapshot_checksum_changes(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    manifest = tmp_path / "manifest.json"
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    payload["published_run"]["model_run_id"] = "tampered"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    manifest.write_text(
        SNAPSHOT.with_name("manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="checksum"):
        load_serving_repository(snapshot)
