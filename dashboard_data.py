"""Read and validate the small, inference-free dashboard data contract."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pandas as pd

from serving_repository import load_serving_repository, overlay_served_forecasts


RESTAURANT_LABELS = {
    "all": "All restaurants",
    "downtown": "Downtown",
    "fscoffee": "FS Coffee",
    "hurricane": "Hurricane",
    "springdale": "Springdale",
}


@dataclass(frozen=True)
class DashboardBundle:
    daily: pd.DataFrame
    replay: pd.DataFrame
    summaries: pd.DataFrame
    disturbances: pd.DataFrame
    manifest: dict[str, object]
    validation: dict[str, object]


def load_bundle(
    bundle_dir: str | Path,
    *,
    serving_snapshot_path: str | Path | None = None,
) -> DashboardBundle:
    """Load only published dashboard artifacts and fail closed on bad data."""

    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    validation = json.loads(
        (root / "validation.json").read_text(encoding="utf-8")
    )
    if validation.get("status") != "valid":
        raise ValueError("The published dashboard bundle has not passed validation")
    daily = pd.read_csv(
        root / "daily_forecasts.csv",
        parse_dates=["forecast_origin", "target_date"],
    )
    if "baseline_median" not in daily:
        raise ValueError(
            "Daily forecasts do not contain the no-disturbance historical baseline"
        )
    if serving_snapshot_path is not None:
        repository = load_serving_repository(serving_snapshot_path)
        daily = overlay_served_forecasts(daily, repository)
        scaled_restaurants = {
            item["restaurant_id"]
            for item in repository.payload["restaurant_scales"]
        }
        manifest = {
            **manifest,
            "serving_snapshot": {
                "contract_version": repository.payload["contract_version"],
                "model_run_id": repository.model_run_id,
                "published_at": repository.payload["published_run"]["published_at"],
                "restaurant_scales": {
                    restaurant: repository.restaurant_scale(restaurant)
                    for restaurant in scaled_restaurants
                },
            },
        }
    replay = pd.read_csv(
        root / "forecast_replay.csv",
        parse_dates=["forecast_origin", "target_date"],
    )
    summaries = pd.read_csv(root / "restaurant_summary.csv")
    disturbances = pd.read_csv(
        root / "disturbances.csv",
        parse_dates=[
            "start_date",
            "end_date",
            "start_lower",
            "start_upper",
            "end_lower",
            "end_upper",
        ],
    )
    expected = set(RESTAURANT_LABELS)
    if set(summaries["restaurant"]) != expected:
        raise ValueError("The bundle does not contain the expected restaurant views")
    if set(daily["restaurant"]) != expected:
        raise ValueError("Daily forecasts do not contain the expected restaurant views")
    if set(replay["restaurant"]) != expected - {"all"}:
        raise ValueError("Forecast replay does not contain the four restaurant views")
    replay_counts = replay.groupby(["restaurant", "forecast_origin"]).size()
    if not replay_counts.eq(14).all():
        raise ValueError("Each replay vintage must contain exactly 14 forecast days")
    if not set(disturbances["restaurant"]).issubset(expected - {"all"}):
        raise ValueError("Disturbance history contains an unknown restaurant")
    if int(validation.get("forecast_replay_rows", -1)) != len(replay):
        raise ValueError("Forecast replay row count does not match validation")
    return DashboardBundle(daily, replay, summaries, disturbances, manifest, validation)


def restaurant_summary(bundle: DashboardBundle, restaurant: str) -> pd.Series:
    rows = bundle.summaries.loc[bundle.summaries["restaurant"].eq(restaurant)]
    if len(rows) != 1:
        raise ValueError(f"Expected one summary row for {restaurant}")
    return rows.iloc[0]


def forecast_rows(bundle: DashboardBundle, restaurant: str) -> pd.DataFrame:
    return bundle.daily.loc[
        bundle.daily["restaurant"].eq(restaurant)
        & bundle.daily["period"].eq("forecast_horizon")
    ].sort_values("target_date")


def recent_rows(bundle: DashboardBundle, restaurant: str) -> pd.DataFrame:
    return bundle.daily.loc[
        bundle.daily["restaurant"].eq(restaurant)
        & bundle.daily["period"].eq("recent_performance")
    ].sort_values("target_date")


def forecast_replay_origins(
    bundle: DashboardBundle, restaurant: str
) -> list[pd.Timestamp]:
    """Return the forecast issue dates preserved in the public bundle.

    The current bundle carries one evaluated prior forecast and the current
    forward forecast. Future publishers can append additional vintages without
    requiring a dashboard code change.
    """

    rows = bundle.replay.loc[bundle.replay["restaurant"].eq(restaurant)]
    return sorted(pd.Timestamp(value) for value in rows["forecast_origin"].unique())


def forecast_replay_rows(
    bundle: DashboardBundle,
    restaurant: str,
    forecast_origin: pd.Timestamp,
) -> pd.DataFrame:
    """Return one saved forecast exactly as published for replay."""

    origin = pd.Timestamp(forecast_origin)
    rows = bundle.replay.loc[
        bundle.replay["restaurant"].eq(restaurant)
        & bundle.replay["forecast_origin"].eq(origin)
    ].sort_values("target_date")
    if rows.empty:
        raise ValueError(
            f"No saved forecast for {restaurant} issued {origin.date()}"
        )
    return rows.copy()


def forecast_replay_history(
    bundle: DashboardBundle, restaurant: str
) -> pd.DataFrame:
    """Return every saved replay vintage for one restaurant."""

    return bundle.replay.loc[
        bundle.replay["restaurant"].eq(restaurant)
    ].sort_values(["forecast_origin", "target_date"]).copy()


def history_rows(bundle: DashboardBundle, restaurant: str) -> pd.DataFrame:
    """Return published one-step-ahead historical model estimates."""

    return bundle.daily.loc[
        bundle.daily["restaurant"].eq(restaurant)
        & bundle.daily["period"].eq("historical_model_estimate")
    ].sort_values("target_date")


def disturbance_rows(bundle: DashboardBundle, restaurant: str) -> pd.DataFrame:
    rows = bundle.disturbances
    if restaurant != "all":
        rows = rows.loc[rows["restaurant"].eq(restaurant)]
    rows = rows.copy()
    rows["restaurant_label"] = rows["restaurant"].map(RESTAURANT_LABELS)
    rows["kind_label"] = rows["disturbance_type"].map(
        {"event": "Event", "regime": "Long-lasting shift"}
    )
    rows["direction_label"] = rows["direction"].map(
        {
            "positive": "Higher sales",
            "negative": "Lower sales",
            "unknown": "Sales shift",
        }
    )
    rows["evidence_label"] = rows["support_probability"].map(evidence_label)
    rows["evidence_explanation"] = rows["support_probability"].map(
        evidence_explanation
    )
    rows["duration_label"] = rows["duration_days"].map(duration_label)
    rows["impact_label"] = rows.apply(impact_label, axis=1)
    return rows.sort_values(["end_date", "restaurant"], ascending=[False, True])


def evidence_label(value: float) -> str:
    if pd.isna(value):
        return "Selected shift"
    if value >= 0.70:
        return "Strong evidence"
    if value >= 0.35:
        return "Moderate evidence"
    return "Limited evidence"


def evidence_explanation(value: float) -> str:
    """Plain-language meaning of the evidence labels shown to managers."""

    if pd.isna(value):
        return (
            "This longer sales shift was selected for this synthetic pipeline "
            "demonstration."
        )
    if value >= 0.70:
        return "This event appeared in at least 70% of the plausible event histories."
    if value >= 0.35:
        return "This event appeared in 35% to 69% of the plausible event histories."
    return "This event appeared in fewer than 35% of the plausible event histories."


def duration_label(value: float) -> str:
    if pd.isna(value):
        return "Duration unavailable"
    days = int(value)
    return f"{days} day" if days == 1 else f"{days} days"


def _signed_money(value: float) -> str:
    if pd.isna(value):
        return "—"
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _signed_percent(value: float) -> str:
    if pd.isna(value):
        return "—"
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.1f}%"


def impact_label(row: pd.Series) -> str:
    """Summarize the conditional daily effect without hiding its uncertainty."""

    dollar_low = row.get("effect_low_dollars_per_day")
    dollar_high = row.get("effect_high_dollars_per_day")
    percent_low = row.get("effect_low_percent")
    percent_high = row.get("effect_high_percent")
    if pd.isna(dollar_low) or pd.isna(dollar_high):
        return "Impact estimate unavailable"
    return (
        f"Likely daily impact when active: {_signed_money(dollar_low)} to "
        f"{_signed_money(dollar_high)} "
        f"({_signed_percent(percent_low)} to {_signed_percent(percent_high)})"
    )
