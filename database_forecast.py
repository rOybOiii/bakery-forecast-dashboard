"""Database-backed site forecast display for the local development app."""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text

from database_event_planning import (
    load_implemented_event_effects,
    render_event_imputation_drawer,
)
from database_marketing_workspace import render_marketing_plan_drawer
from gateway_client import active_gateway_scope, application_call


def load_current_site_forecast(site_id: int) -> dict[str, Any] | None:
    """Load only currently usable observations and model outputs for one site."""

    if active_gateway_scope() is not None:
        return application_call("workspace", "forecast.load_current", site_id)

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
                    SELECT f.*, a.run_id, r.input_artifact_id,
                        m.name AS metric_name,
                        recovery.id AS provenance_recovery_id,
                        recovery.anchor_run_id AS provenance_anchor_run_id,
                        recovery.anchor_dataset_id AS provenance_anchor_dataset_id,
                        recovery.manifest AS provenance_recovery_manifest
                    FROM current_forecast f
                    JOIN artifact a ON a.id = f.artifact_id
                    JOIN model_run r ON r.id = a.run_id
                    JOIN metric m ON m.id = f.metric_id
                    LEFT JOIN provenance_change_recovery recovery
                      ON recovery.recovery_run_id = r.id
                    WHERE f.site_id = :site_id
                    ORDER BY f.published_at DESC, a.run_id DESC, f.id DESC
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
                    SELECT o.date, o.value
                    FROM observation o
                    JOIN artifact input_artifact
                      ON input_artifact.id = :input_artifact_id
                    JOIN data_review review
                      ON review.id = input_artifact.review_id
                     AND review.dataset_id = o.dataset_id
                    WHERE o.site_id = :site_id AND review.metric_id = :metric_id
                    ORDER BY o.date DESC
                    LIMIT 90
                    """
                ),
                {
                    "site_id": site_id,
                    "metric_id": forecast["metric_id"],
                    "input_artifact_id": forecast["input_artifact_id"],
                },
            ).mappings().all()
            events = connection.execute(
                text(
                    """
                    SELECT e.start_date, e.end_date, e.support,
                        e.direction, e.status
                    FROM current_event e
                    JOIN event_inference_lineage lineage
                      ON lineage.event_id = e.id
                    WHERE e.run_id = :run_id
                      AND e.site_id = :site_id AND e.metric_id = :metric_id
                    ORDER BY e.start_date DESC, e.id DESC
                    LIMIT 12
                    """
                ),
                {
                    "run_id": forecast["run_id"],
                    "site_id": site_id,
                    "metric_id": forecast["metric_id"],
                },
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
            marketing_score = connection.execute(
                text(
                    """
                    SELECT score.id, score.scoring_run_id,
                        score.source_forecast_run_id, score.qualification_id,
                        score.plan_id, score.health_status, score.summary,
                        score.published_at, program.lifecycle,
                        program.health_status AS program_health_status,
                        program.health_summary, plan.budget AS planned_spend
                    FROM mmm_program program
                    LEFT JOIN mmm_operational_score score
                      ON score.program_id = program.id
                     AND score.state = 'current'
                     AND score.source_forecast_run_id = :run_id
                    LEFT JOIN spend_plan plan ON plan.id = score.plan_id
                    WHERE program.site_id = :site_id
                      AND program.metric_id = :metric_id
                    ORDER BY score.published_at DESC NULLS LAST,
                        score.id DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {
                    "site_id": site_id,
                    "metric_id": forecast["metric_id"],
                    "run_id": forecast["run_id"],
                },
            ).mappings().one_or_none()
            marketing_points = []
            if marketing_score is not None and marketing_score["id"] is not None:
                marketing_points = connection.execute(
                    text(
                        """
                        SELECT date, base_low, base_median, base_high,
                            carryover_low, carryover_median, carryover_high,
                            planned_lift_low, planned_lift_median,
                            planned_lift_high, marketing_lift_low,
                            marketing_lift_median, marketing_lift_high,
                            combined_low, combined_median, combined_high
                        FROM mmm_operational_score_point
                        WHERE score_id = :score_id
                        ORDER BY date
                        """
                    ),
                    {"score_id": marketing_score["id"]},
                ).mappings().all()
            event_plan = load_implemented_event_effects(int(forecast["id"]))
    finally:
        engine.dispose()
    return {
        "forecast": dict(forecast),
        "predictions": [dict(row) for row in predictions],
        "observations": [dict(row) for row in reversed(observations)],
        "events": [dict(row) for row in events],
        "diagnostics": [dict(row) for row in diagnostics],
        "marketing_score": (
            dict(marketing_score) if marketing_score is not None else None
        ),
        "marketing_points": [dict(row) for row in marketing_points],
        "event_plan": event_plan,
    }


def _money(value: float) -> str:
    return f"${value:,.0f}"


def render_current_site_forecast(
    data: dict[str, Any], *, org_id: int, site_name: str, actor_id: int
) -> None:
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
    if forecast.get("provenance_recovery_id") is not None:
        manifest = dict(forecast.get("provenance_recovery_manifest") or {})
        changed_rows = list(manifest.get("changed_rows") or [])
        changed_dates = [str(row.get("date")) for row in changed_rows if row.get("date")]
        st.info(
            "This forecast was rebuilt after an accepted provenance change. "
            f"Recovery #{forecast['provenance_recovery_id']} used forecast job "
            f"#{forecast['provenance_anchor_run_id']} as its input anchor"
            + (
                f" and corrected {', '.join(changed_dates)}."
                if changed_dates
                else "."
            )
        )

    marketing = data.get("marketing_score")
    marketing_points = pd.DataFrame(data.get("marketing_points") or [])
    event_plan = data.get("event_plan")
    event_points = pd.DataFrame((event_plan or {}).get("points") or [])
    if event_points.empty:
        event_points = pd.DataFrame(
            {
                "date": pd.Series(dtype="datetime64[ns]"),
                "event_low": pd.Series(dtype="float64"),
                "event_median": pd.Series(dtype="float64"),
                "event_high": pd.Series(dtype="float64"),
            }
        )
    else:
        event_points["date"] = pd.to_datetime(event_points["date"])
        for column in ("low", "median", "high"):
            event_points[column] = pd.to_numeric(event_points[column])
        event_points = event_points.rename(
            columns={
                "low": "event_low",
                "median": "event_median",
                "high": "event_high",
            }
        )
    operational = marketing is not None and not marketing_points.empty
    if operational:
        marketing_points["date"] = pd.to_datetime(marketing_points["date"])
        numeric_columns = [
            column for column in marketing_points.columns if column != "date"
        ]
        marketing_points[numeric_columns] = marketing_points[numeric_columns].apply(
            pd.to_numeric
        )
        display_forecast = marketing_points.merge(event_points, on="date", how="left")
        for column in ("event_low", "event_median", "event_high"):
            if column not in display_forecast:
                display_forecast[column] = 0.0
            display_forecast[column] = display_forecast[column].fillna(0.0)
        display_forecast["presented_low"] = (
            display_forecast["combined_low"] + display_forecast["event_low"]
        )
        display_forecast["presented_median"] = (
            display_forecast["combined_median"] + display_forecast["event_median"]
        )
        display_forecast["presented_high"] = (
            display_forecast["combined_high"] + display_forecast["event_high"]
        )
    else:
        display_forecast = predictions.merge(event_points, on="date", how="left")
        for column in ("event_low", "event_median", "event_high"):
            if column not in display_forecast:
                display_forecast[column] = 0.0
            display_forecast[column] = display_forecast[column].fillna(0.0)
        display_forecast["presented_low"] = (
            display_forecast["low"] + display_forecast["event_low"]
        )
        display_forecast["presented_median"] = (
            display_forecast["median"] + display_forecast["event_median"]
        )
        display_forecast["presented_high"] = (
            display_forecast["high"] + display_forecast["event_high"]
        )
    total = float(display_forecast["presented_median"].sum())
    average = float(display_forecast["presented_median"].mean())
    metrics = st.columns(4)
    metrics[0].metric(f"Next {len(predictions)} days", _money(total))
    metrics[1].metric("Average forecast day", _money(average))
    if operational:
        organic_total = float(marketing_points["base_median"].sum())
        lift_total = float(marketing_points["marketing_lift_median"].sum())
        metrics[2].metric(
            "Marketing influence",
            _money(lift_total),
            delta=f"{_money(float(marketing_points['planned_lift_median'].sum()))} planned",
        )
        metrics[3].metric("Organic baseline", _money(organic_total))
    else:
        metrics[2].metric(
            "Forecast origin", pd.Timestamp(forecast["origin"]).strftime("%b %-d")
        )
        metrics[3].metric(
            "Forecast through", predictions["date"].max().strftime("%b %-d")
        )

    st.markdown('<div class="section-title">Recent sales and the next two weeks</div>', unsafe_allow_html=True)
    if operational:
        st.markdown(
            '<div class="section-note">The dashed line is the marketing-clean organic baseline. The green line adds carryover from prior spend and expected lift from the implemented plan. The shaded band is the full scored uncertainty.</div>',
            unsafe_allow_html=True,
        )
        band = (
            alt.Chart(display_forecast)
            .mark_area(color="#8fb9a8", opacity=0.32)
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("presented_low:Q", title="Sales"),
                y2="presented_high:Q",
            )
        )
        organic_line = (
            alt.Chart(display_forecast)
            .mark_line(color="#6f8790", strokeDash=[6, 5], strokeWidth=2)
            .encode(
                x="date:T",
                y="base_median:Q",
                tooltip=[
                    alt.Tooltip("date:T", title="Date"),
                    alt.Tooltip("base_median:Q", title="Organic baseline", format="$,.0f"),
                ],
            )
        )
        forecast_line = (
            alt.Chart(display_forecast)
            .mark_line(color="#2f6b55", strokeWidth=3, point=True)
            .encode(
                x="date:T",
                y="presented_median:Q",
                tooltip=[
                    alt.Tooltip("date:T", title="Date"),
                    alt.Tooltip("presented_median:Q", title="Scored forecast", format="$,.0f"),
                    alt.Tooltip("base_median:Q", title="Organic baseline", format="$,.0f"),
                    alt.Tooltip("carryover_median:Q", title="Carryover", format="$,.0f"),
                    alt.Tooltip("planned_lift_median:Q", title="Planned lift", format="$,.0f"),
                    alt.Tooltip("event_median:Q", title="Event effect", format="$,.0f"),
                ],
            )
        )
        chart = band + organic_line + forecast_line
    else:
        st.markdown(
            '<div class="section-note">Actual sales come from the approved data snapshot used by this run. The shaded band is this run’s 90% predictive interval.</div>',
            unsafe_allow_html=True,
        )
        band = (
            alt.Chart(display_forecast)
            .mark_area(color="#8fb9a8", opacity=0.32)
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("presented_low:Q", title="Sales"),
                y2="presented_high:Q",
            )
        )
        forecast_line = (
            alt.Chart(display_forecast)
            .mark_line(color="#2f6b55", strokeWidth=3)
            .encode(
                x="date:T",
                y="presented_median:Q",
                tooltip=[
                    "date:T",
                    "presented_median:Q",
                    "event_median:Q",
                    "presented_low:Q",
                    "presented_high:Q",
                ],
            )
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

    if operational:
        carryover = float(marketing_points["carryover_median"].sum())
        planned = float(marketing_points["planned_lift_median"].sum())
        with st.container(border=True):
            st.markdown("**Implemented marketing plan**")
            plan_copy = (
                "No future spend is included; the current influence is carryover only."
                if marketing.get("plan_id") is None
                else (
                    f"Plan #{marketing['plan_id']} is scored against forecast run "
                    f"#{marketing['source_forecast_run_id']}."
                )
            )
            st.caption(plan_copy)
            breakdown = st.columns(4)
            breakdown[0].metric("Existing carryover", _money(carryover))
            breakdown[1].metric("Planned lift", _money(planned))
            breakdown[2].metric(
                "Planned spend",
                _money(float(marketing.get("planned_spend") or 0.0)),
            )
            breakdown[3].metric(
                "MMM health", str(marketing.get("health_status") or "watch").title()
            )
            plan_column, workspace_column = st.columns(2)
            if plan_column.button(
                "Review or change plan",
                type="primary",
                width="stretch",
            ):
                st.session_state.mmm_plan_drawer_open = True
                st.rerun()
            if workspace_column.button(
                "Actual spend and Learning",
                width="stretch",
            ):
                st.session_state.show_marketing_workspace = True
                st.rerun()
    elif marketing is not None:
        lifecycle = str(marketing.get("lifecycle") or "learning").replace("_", " ")
        with st.container(border=True):
            st.markdown("**Marketing learning**")
            st.caption(
                f"MMM is {lifecycle}. This forecast remains organic-only until an "
                "eligible fit and its scored forecast publish together."
            )
            if st.button("Review marketing learning", type="primary"):
                st.session_state.show_marketing_workspace = True
                st.rerun()

    with st.container(border=True):
        st.markdown("**Upcoming events**")
        placements = list((event_plan or {}).get("placements") or [])
        if placements:
            total_event_effect = float(display_forecast["event_median"].sum())
            st.caption(
                f"{len(placements)} event placement(s) are implemented against "
                f"forecast #{forecast['id']}; expected effect is "
                f"{_money(total_event_effect)}."
            )
        else:
            st.caption(
                "No known future events are imputed into this forecast. Add one "
                "from a reviewed event family."
            )
        if st.button("Review or impute events", type="primary"):
            st.session_state.event_imputation_drawer_open = True
            st.rerun()

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

    if st.session_state.get("event_imputation_drawer_open"):
        render_event_imputation_drawer(
            org_id=org_id,
            site_id=int(forecast["site_id"]),
            metric_id=int(forecast["metric_id"]),
            forecast_id=int(forecast["id"]),
            actor_id=actor_id,
        )
    if st.session_state.get("mmm_plan_drawer_open"):
        render_marketing_plan_drawer(
            org_id=org_id,
            site_id=int(forecast["site_id"]),
            metric_id=int(forecast["metric_id"]),
            actor_id=actor_id,
        )
