"""Read the versioned Credent backend snapshot without importing model code."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


CONTRACT_VERSION = "credent_backend_snapshot_v1"


@dataclass(frozen=True)
class ServingRepository:
    """Small file-backed repository that can later be replaced by PostgreSQL."""

    payload: dict[str, Any]
    manifest: dict[str, Any]

    @property
    def model_run_id(self) -> str:
        return str(self.payload["published_run"]["model_run_id"])

    def forecast_frame(self, restaurant_id: str) -> pd.DataFrame:
        candidates = [
            item
            for item in self.payload["forecast_vintages"]
            if item["restaurant_id"] == restaurant_id
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one published forecast vintage for {restaurant_id}"
            )
        vintage = candidates[0]
        frame = pd.DataFrame(vintage["points"])
        frame["forecast_origin"] = pd.Timestamp(vintage["forecast_origin"])
        frame["target_date"] = pd.to_datetime(frame["target_date"])
        frame["restaurant"] = restaurant_id
        frame["period"] = "forecast_horizon"
        frame["run_id"] = self.model_run_id
        frame["unit"] = vintage["unit"]
        frame["serving_contract"] = CONTRACT_VERSION
        return frame

    def event_family_records(self, restaurant_id: str) -> list[dict[str, Any]]:
        families = {
            item["family_id"]: item for item in self.payload["event_families"]
        }
        occurrences = {
            item["occurrence_id"]: item
            for item in self.payload["event_occurrences"]
            if item["restaurant_id"] == restaurant_id
        }
        records: list[dict[str, Any]] = []
        for estimate in self.payload["family_estimates"]:
            if estimate["restaurant_id"] != restaurant_id:
                continue
            family = families.get(estimate["family_id"])
            if family is None:
                raise ValueError("Published family estimate references an unknown family")
            source = [
                occurrences[item]
                for item in estimate["source_occurrence_ids"]
                if item in occurrences
            ]
            if len(source) != len(estimate["source_occurrence_ids"]):
                raise ValueError("Published family estimate is missing source occurrences")
            records.append(
                {"family": family, "estimate": estimate, "occurrences": source}
            )
        return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_serving_repository(snapshot_path: str | Path) -> ServingRepository:
    """Load only an atomically published snapshot and fail closed on mismatch."""

    path = Path(snapshot_path)
    manifest_path = path.with_name("manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("Unsupported Credent serving contract")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("Serving manifest and snapshot contracts disagree")
    if manifest.get("status") != "valid":
        raise ValueError("Serving snapshot has not passed publication validation")
    if manifest.get("snapshot_sha256") != _sha256(path):
        raise ValueError("Serving snapshot checksum does not match its manifest")
    published = payload.get("published_run", {})
    if published.get("status") != "published":
        raise ValueError("Dashboard cannot consume an unpublished model run")
    if published.get("model_run_id") != manifest.get("model_run_id"):
        raise ValueError("Serving manifest points to a different model run")
    return ServingRepository(payload=payload, manifest=manifest)


def overlay_served_forecasts(
    daily: pd.DataFrame,
    repository: ServingRepository,
) -> pd.DataFrame:
    """Replace matching horizon summaries while preserving richer bundle fields."""

    result = daily.copy()
    restaurants = {
        item["restaurant_id"]
        for item in repository.payload["forecast_vintages"]
        if item["unit"] == "dollars"
    }
    for restaurant in restaurants:
        served = repository.forecast_frame(restaurant)
        for row in served.itertuples(index=False):
            mask = (
                result["restaurant"].eq(restaurant)
                & result["period"].eq("forecast_horizon")
                & result["target_date"].eq(row.target_date)
            )
            if int(mask.sum()) != 1:
                raise ValueError(
                    f"Serving forecast does not align with bundle day {row.target_date:%Y-%m-%d}"
                )
            result.loc[mask, ["q05", "median", "q95", "lead_days", "run_id"]] = [
                row.q05,
                row.median,
                row.q95,
                row.lead_days,
                row.run_id,
            ]
            result.loc[mask, "forecast_origin"] = row.forecast_origin
    return result
