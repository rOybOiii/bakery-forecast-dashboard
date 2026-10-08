"""Build cutoff-only weekly replay vintages for the synthetic dashboard.

These rows are an interface backfill, not archived Forecast 2 production runs.
Every backfilled forecast uses only sales reported on or before its issue date.
The current forward Forecast 2 vintage is preserved from the published bundle.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "data" / "dashboard_bundle_v1"
DAILY_PATH = BUNDLE / "daily_forecasts.csv"
REPLAY_PATH = BUNDLE / "forecast_replay.csv"
RESTAURANTS = ("downtown", "fscoffee", "hurricane", "springdale")
QUANTILES = {
    "q025": 0.025,
    "q05": 0.05,
    "q20": 0.20,
    "q25": 0.25,
    "q35": 0.35,
    "median": 0.50,
    "q65": 0.65,
    "q75": 0.75,
    "q80": 0.80,
    "q95": 0.95,
    "q975": 0.975,
}


def _robust_scale(values: pd.Series, fallback: float) -> float:
    clean = values.dropna().astype(float)
    if len(clean) < 8:
        return fallback
    center = float(clean.median())
    mad = float((clean - center).abs().median())
    scale = 1.4826 * mad
    return max(scale, fallback * 0.35)


def _cutoff_forecast(
    history: pd.DataFrame,
    origin: pd.Timestamp,
    actual_lookup: pd.DataFrame,
) -> pd.DataFrame:
    observed = history.loc[
        history["target_date"].le(origin)
        & history["actual_available"].astype(bool)
        & history["actual_sales"].notna()
    ].copy()
    observed["weekday"] = observed["target_date"].dt.dayofweek
    if len(observed) < 56:
        raise ValueError(f"Insufficient pre-cutoff history at {origin.date()}")

    recent_level = float(observed.tail(28)["actual_sales"].mean())
    prior_level = float(observed.iloc[-56:-28]["actual_sales"].mean())
    level_adjustment = float(
        np.clip(0.35 * (recent_level - prior_level), -0.12 * recent_level, 0.12 * recent_level)
    )

    historical_expected = observed.groupby("weekday")["actual_sales"].transform(
        lambda values: values.shift().rolling(8, min_periods=4).mean()
    )
    residuals = observed["actual_sales"] - historical_expected
    fallback_scale = max(250.0, recent_level * 0.10)
    residual_scale = _robust_scale(residuals.tail(112), fallback_scale)

    future_dates = pd.date_range(origin + pd.DateOffset(days=1), periods=14, freq="D")
    rows: list[dict[str, object]] = []
    for lead, target in enumerate(future_dates, start=1):
        weekday_history = observed.loc[
            observed["weekday"].eq(target.dayofweek), "actual_sales"
        ].tail(10)
        weights = np.geomspace(0.35, 1.0, len(weekday_history))
        weekday_level = float(np.average(weekday_history, weights=weights))
        median = max(0.0, weekday_level + level_adjustment)
        predictive_sd = residual_scale * np.sqrt(1.0 + 0.025 * lead)
        row: dict[str, object] = {
            "restaurant": str(history["restaurant"].iloc[0]),
            "forecast_origin": origin,
            "target_date": target,
            "predictive_sd": predictive_sd,
            "baseline_median": median,
            "lead_days": lead,
            "run_id": f"cutoff_backfill_{origin.date()}_{history['restaurant'].iloc[0]}",
            "vintage_kind": "cutoff_backfill_demo",
        }
        for field, probability in QUANTILES.items():
            row[field] = max(
                0.0,
                median + NormalDist().inv_cdf(probability) * predictive_sd,
            )
        actual = actual_lookup.loc[actual_lookup["target_date"].eq(target)]
        available = bool(
            len(actual)
            and actual["actual_available"].iloc[0]
            and pd.notna(actual["actual_sales"].iloc[0])
        )
        row["actual_available"] = available
        row["actual_sales"] = (
            float(actual["actual_sales"].iloc[0]) if available else np.nan
        )
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    daily = pd.read_csv(
        DAILY_PATH,
        parse_dates=["forecast_origin", "target_date"],
    )
    history = daily.loc[daily["period"].eq("historical_model_estimate")]
    current_origin = pd.Timestamp(
        daily.loc[daily["period"].eq("forecast_horizon"), "forecast_origin"].max()
    )
    origins = pd.date_range(
        current_origin - pd.DateOffset(days=90),
        current_origin - pd.DateOffset(days=1),
        freq="D",
    )

    parts: list[pd.DataFrame] = []
    for restaurant in RESTAURANTS:
        restaurant_history = history.loc[history["restaurant"].eq(restaurant)].copy()
        for origin in origins:
            parts.append(
                _cutoff_forecast(
                    restaurant_history,
                    pd.Timestamp(origin),
                    restaurant_history,
                )
            )

        current = daily.loc[
            daily["restaurant"].eq(restaurant)
            & daily["period"].eq("forecast_horizon")
        ].copy()
        current["vintage_kind"] = "archived_forecast_2"
        parts.append(
            current[
                [
                    "restaurant",
                    "forecast_origin",
                    "target_date",
                    *QUANTILES,
                    "predictive_sd",
                    "baseline_median",
                    "lead_days",
                    "actual_sales",
                    "actual_available",
                    "run_id",
                    "vintage_kind",
                ]
            ]
        )

    replay = pd.concat(parts, ignore_index=True).sort_values(
        ["restaurant", "forecast_origin", "target_date"], ignore_index=True
    )
    replay.to_csv(REPLAY_PATH, index=False, date_format="%Y-%m-%d")

    manifest_path = BUNDLE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["forecast_replay"] = {
        "file": REPLAY_PATH.name,
        "origins_per_restaurant": int(replay["forecast_origin"].nunique()),
        "backfill_frequency": "daily",
        "backfill_method": (
            "cutoff-only recency-weighted weekday forecast for interface qualification; "
            "replace with archived Forecast 2 vintages in production"
        ),
        "uses_future_information": False,
        "current_vintage_source": "archived Forecast 2 output",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    validation_path = BUNDLE / "validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["forecast_replay_rows"] = int(len(replay))
    validation["forecast_replay_origins"] = int(
        replay["forecast_origin"].nunique()
    )
    validation_path.write_text(
        json.dumps(validation, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {len(replay)} rows across "
        f"{replay['forecast_origin'].nunique()} replay origins"
    )


if __name__ == "__main__":
    main()
