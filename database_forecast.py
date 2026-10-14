"""Database-backed site forecast display for the local development app."""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text


def load_current_site_forecast(site_id: int) -> dict[str, Any] | None:
    """Load only currently usable observations and model outputs for one site."""

    try:
        from credent_db import make_engine
    except ImportError:
        return None
    engine = make_engine()
    try:
        with engine.connect() as connection:
            forecast = connection.execute(
                text(
                    """
                    SELECT f.*, a.run_id, m.name AS metric_name
                    FROM current_forecast f
                    JOIN artifact a ON a.id = f.artifact_id
                    JOIN metric m ON m.id = f.metric_id
                    WHERE f.site_id = :site_id
                    ORDER BY f.origin DESC, f.id DESC
                    LIMIT 1
                    """
                ),
                {"site_id": site_id},
            ).mappings().one_or_none()
            if forecast is None:
                return None
            predictions = connection.execute(
                text(
                    """
                    SELECT date, lead, low, median, high
                    FROM prediction
                    WHERE forecast_id = :forecast_id
                    ORDER BY lead
                    """
                ),
                {"forecast_id": forecast["id"]},
            ).mappings().all()
            observations = connection.execute(
                text(
                    """
                    SELECT date, value
                    FROM current_observation
                    WHERE site_id = :site_id AND metric_id = :metric_id
                    ORDER BY date DESC
                    LIMIT 90
                    """
                ),
                {"site_id": site_id, "metric_id": forecast["metric_id"]},
            ).mappings().all()
            events = connection.execute(
                text(
                    """
                    SELECT start_date, end_date, support, direction, status
                    FROM current_event
                    WHERE site_id = :site_id AND metric_id = :metric_id
                    ORDER BY start_date DESC, id DESC
                    LIMIT 12
                    """
                ),
                {"site_id": site_id, "metric_id": forecast["metric_id"]},
            ).mappings().all()
            diagnostics = connection.execute(
                text(
                    """
                    SELECT phase, status, summary
                    FROM diagnostic_report
                    WHERE run_id = :run_id
                    ORDER BY id
                    """
                ),
                {"run_id": forecast["run_id"]},
            ).mappings().all()
    finally:
        engine.dispose()
    return {
        "forecast": dict(forecast),
        "predictions": [dict(row) for row in predictions],
        "observations": [dict(row) for row in reversed(observations)],
        "events": [dict(row) for row in events],
        "diagnostics": [dict(row) for row in diagnostics],
    }


def _money(value: float) -> str:
    return f"${value:,.0f}"


def render_current_site_forecast(data: dict[str, Any], *, site_name: str) -> None:
    """Render a compact database-backed operational forecast."""

    forecast = data["forecast"]
    predictions = pd.DataFrame(data["predictions"])
    predictions["date"] = pd.to_datetime(predictions["date"])
    for column in ("low", "median", "high"):
        predictions[column] = pd.to_numeric(predictions[column])
    observations = pd.DataFrame(data["observations"])
    if not observations.empty:
        observations["date"] = pd.to_datetime(observations["date"])
        observations["value"] = pd.to_numeric(observations["value"])

    st.markdown('<div class="dashboard-kicker">Daily production planning</div>', unsafe_allow_html=True)
    st.markdown(f'<h1 class="dashboard-title">{site_name}</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="dashboard-subtitle">Current published forecast · run #{forecast["run_id"]}</div>',
        unsafe_allow_html=True,
    )

    total = float(predictions["median"].sum())
    average = float(predictions["median"].mean())
    metrics = st.columns(4)
    metrics[0].metric(f"Next {len(predictions)} days", _money(total))
    metrics[1].metric("Average forecast day", _money(average))
    metrics[2].metric("Forecast origin", pd.Timestamp(forecast["origin"]).strftime("%b %-d"))
    metrics[3].metric("Forecast through", predictions["date"].max().strftime("%b %-d"))

    st.markdown('<div class="section-title">Recent sales and the next two weeks</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Actual sales come from current observations. The shaded band is the current 90% predictive interval.</div>',
        unsafe_allow_html=True,
    )
    band = (
        alt.Chart(predictions)
        .mark_area(color="#8fb9a8", opacity=0.32)
        .encode(x=alt.X("date:T", title=None), y=alt.Y("low:Q", title="Sales"), y2="high:Q")
    )
    forecast_line = (
        alt.Chart(predictions)
        .mark_line(color="#2f6b55", strokeWidth=3)
        .encode(x="date:T", y="median:Q", tooltip=["date:T", "median:Q", "low:Q", "high:Q"])
    )
    chart = band + forecast_line
    if not observations.empty:
        actual_line = (
            alt.Chart(observations)
            .mark_line(color="#1d2a24", strokeWidth=2)
            .encode(x="date:T", y="value:Q", tooltip=["date:T", "value:Q"])
        )
        chart = actual_line + chart
    st.altair_chart(chart.properties(height=390), use_container_width=True)

    warnings = [row for row in data["diagnostics"] if row["status"] == "warning"]
    if warnings:
        st.info(
            "This development run completed with visible diagnostic warnings. "
            "Open the run details above to review them before treating it as production-qualified."
        )

    if data["events"]:
        st.markdown('<div class="section-title">Detected events from this run</div>', unsafe_allow_html=True)
        event_frame = pd.DataFrame(data["events"]).rename(
            columns={
                "start_date": "Start",
                "end_date": "End",
                "support": "Support",
                "direction": "Direction",
                "status": "Status",
            }
        )
        event_frame["Support"] = pd.to_numeric(event_frame["Support"]).map(lambda value: f"{value:.0%}")
        st.dataframe(event_frame, hide_index=True, use_container_width=True)
