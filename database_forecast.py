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
from onboarding_workspace import _enqueue_fresh_first_forecast


def _chart_palette() -> dict[str, str]:
    if getattr(getattr(st.context, "theme", None), "type", "light") == "dark":
        return {
            "forecast": "#DC8454",
            "band": "#B58D80",
            "actual": "#A6AD7D",
            "baseline": "#9AADB6",
        }
    return {
        "forecast": "#9D4F31",
        "band": "#B66E49",
        "actual": "#596047",
        "baseline": "#4F6068",
    }


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
                        current_review.review_id,
                        m.name AS metric_name,
                        recovery.id AS provenance_recovery_id,
                        recovery.anchor_run_id AS provenance_anchor_run_id,
                        recovery.anchor_dataset_id AS provenance_anchor_dataset_id,
                        recovery.manifest AS provenance_recovery_manifest
                    FROM current_forecast f
                    JOIN artifact a ON a.id = f.artifact_id
                    JOIN model_run r ON r.id = a.run_id
                    JOIN metric m ON m.id = f.metric_id
                    LEFT JOIN LATERAL (
                        SELECT review.id AS review_id
                        FROM data_review review
                        JOIN dataset ON dataset.id = review.dataset_id
                        WHERE review.site_id = f.site_id
                          AND review.metric_id = f.metric_id
                          AND review.status = 'approved'
                          AND dataset.validity_status = 'valid'
                        ORDER BY review.approved_at DESC NULLS LAST, review.id DESC
                        LIMIT 1
                    ) current_review ON true
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
            replay_vintages = connection.execute(
                text(
                    """
                    SELECT f.id, f.origin, f.published_at, a.run_id
                    FROM forecast f
                    JOIN artifact a ON a.id = f.artifact_id
                    JOIN model_run r ON r.id = a.run_id
                    WHERE f.site_id = :site_id
                      AND f.metric_id = :metric_id
                      AND f.status = 'published'
                      AND a.status = 'published'
                      AND a.validity_status = 'valid'
                      AND r.status = 'published'
                      AND r.validity_status = 'valid'
                    ORDER BY f.published_at DESC NULLS LAST, f.id DESC
                    LIMIT 12
                    """
                ),
                {"site_id": site_id, "metric_id": forecast["metric_id"]},
            ).mappings().all()
            replay_ids = [int(item["id"]) for item in replay_vintages]
            replay_points = (
                connection.execute(
                    text(
                        """
                        SELECT forecast_id, date, lead, low, median, high
                        FROM prediction
                        WHERE forecast_id = ANY(:forecast_ids)
                        ORDER BY forecast_id, lead
                        """
                    ),
                    {"forecast_ids": replay_ids},
                ).mappings().all()
                if replay_ids
                else []
            )
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
        "replay_vintages": [dict(row) for row in reversed(replay_vintages)],
        "replay_points": [dict(row) for row in replay_points],
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


def _signed_money(value: float) -> str:
    prefix = "+" if value > 0 else "−" if value < 0 else ""
    return f"{prefix}${abs(value):,.0f}"


@st.fragment(run_every="1s")
def _render_forecast_replay(
    *,
    site_id: int,
    vintages: list[dict[str, Any]],
    points: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    """Render the saved-forecast player used on the site landing page."""

    if not vintages:
        st.caption("Forecast replay will appear after the first forecast is published.")
        return
    index_key = f"forecast_replay_index_{site_id}"
    playing_key = f"forecast_replay_playing_{site_id}"
    if index_key not in st.session_state:
        st.session_state[index_key] = len(vintages) - 1
    if playing_key not in st.session_state:
        st.session_state[playing_key] = False
    if st.session_state[playing_key]:
        next_index = int(st.session_state[index_key]) + 1
        if next_index >= len(vintages):
            st.session_state[playing_key] = False
        else:
            st.session_state[index_key] = next_index

    control, scrubber = st.columns([1, 7], vertical_alignment="bottom")
    if control.button(
        "Pause" if st.session_state[playing_key] else "Play",
        key=f"forecast_replay_toggle_{site_id}",
        use_container_width=True,
    ):
        if int(st.session_state[index_key]) >= len(vintages) - 1:
            st.session_state[index_key] = 0
        st.session_state[playing_key] = not st.session_state[playing_key]
        st.rerun(scope="fragment")

    def issue_label(index: int) -> str:
        value = vintages[index].get("published_at") or vintages[index].get("origin")
        return pd.Timestamp(value).strftime("%b %-d, %Y")

    scrubber.select_slider(
        "Forecast issue date",
        options=list(range(len(vintages))),
        format_func=issue_label,
        key=index_key,
    )
    selected = vintages[int(st.session_state[index_key])]
    forecast_id = int(selected["id"])
    frame = pd.DataFrame(
        row for row in points if int(row["forecast_id"]) == forecast_id
    )
    if frame.empty:
        st.caption("This saved forecast has no prediction points to replay.")
        return
    frame["date"] = pd.to_datetime(frame["date"])
    for column in ("low", "median", "high"):
        frame[column] = pd.to_numeric(frame[column])
    actuals = pd.DataFrame(observations)
    if not actuals.empty:
        actuals["date"] = pd.to_datetime(actuals["date"])
        actuals["value"] = pd.to_numeric(actuals["value"])
        actuals = actuals.loc[
            actuals["date"].between(frame["date"].min(), frame["date"].max())
        ]
    colors = _chart_palette()
    base = alt.Chart(frame).encode(x=alt.X("date:T", title=None))
    replay_chart = base.mark_area(color=colors["band"], opacity=0.24).encode(
        y=alt.Y("low:Q", title="Sales", scale=alt.Scale(zero=False)),
        y2="high:Q",
    ) + base.mark_line(color=colors["forecast"], strokeWidth=3).encode(
        y="median:Q",
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("median:Q", title="Forecast", format="$,.0f"),
            alt.Tooltip("low:Q", title="Low", format="$,.0f"),
            alt.Tooltip("high:Q", title="High", format="$,.0f"),
        ],
    )
    if not actuals.empty:
        replay_chart += alt.Chart(actuals).mark_line(
            color=colors["actual"], strokeWidth=2, point=True
        ).encode(
            x="date:T",
            y="value:Q",
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip("value:Q", title="Actual sales", format="$,.0f"),
            ],
        )
    st.altair_chart(replay_chart.properties(height=285), use_container_width=True)
    st.caption(
        f"Forecast issued {issue_label(int(st.session_state[index_key]))} · "
        f"job #{int(selected['run_id'])}"
    )


def render_current_site_forecast(
    data: dict[str, Any], *, org_id: int, site_name: str, actor_id: int
) -> None:
    """Render a compact database-backed operational forecast."""

    colors = _chart_palette()
    forecast = data["forecast"]
    predictions = pd.DataFrame(data["predictions"])
    predictions["date"] = pd.to_datetime(predictions["date"])
    for column in ("low", "median", "high"):
        predictions[column] = pd.to_numeric(predictions[column])
    observations = pd.DataFrame(data["observations"])
    if not observations.empty:
        observations["date"] = pd.to_datetime(observations["date"])
        observations["value"] = pd.to_numeric(observations["value"])

    forecast_through = predictions["date"].max()
    st.markdown(
        f'<div class="dashboard-kicker">{site_name}</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<h1 class="dashboard-title">Upcoming forecast</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dashboard-subtitle">'
        f'Forecast through {forecast_through:%B %-d, %Y} · '
        f'published from job #{forecast["run_id"]}</div>',
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
    action_columns = st.columns(3, gap="medium")
    with action_columns[0], st.container(border=True):
        st.markdown("### Update forecast")
        st.caption("Add recent sales and refresh the forecast.")
        if st.button(
            "Update sales data",
            type="primary",
            use_container_width=True,
            key=f"landing_update_forecast_{forecast['site_id']}",
        ):
            st.session_state.show_sales_data_workspace = True
            st.rerun()
    with action_columns[1], st.container(border=True):
        st.markdown("### Plan forecast events")
        st.caption("Place known events into this forecast.")
        if st.button(
            "Plan events",
            use_container_width=True,
            key=f"landing_plan_events_{forecast['site_id']}",
        ):
            st.session_state.event_imputation_drawer_open = True
            st.rerun()
    with action_columns[2], st.container(border=True):
        st.markdown("### Marketing plan")
        st.caption("Review or revise the current plan.")
        if st.button(
            "Open marketing",
            use_container_width=True,
            key=f"landing_marketing_{forecast['site_id']}",
        ):
            if marketing is not None:
                st.session_state.mmm_plan_drawer_open = True
            else:
                st.session_state.show_marketing_workspace = True
            st.rerun()

    replay_vintages = list(data.get("replay_vintages") or [])
    replay_points = list(data.get("replay_points") or [])
    previous_total = None
    if len(replay_vintages) > 1:
        prior_id = int(replay_vintages[-2]["id"])
        prior_values = [
            float(row["median"])
            for row in replay_points
            if int(row["forecast_id"]) == prior_id
        ]
        if prior_values:
            previous_total = sum(prior_values)
    range_half_width = (
        float(display_forecast["presented_high"].sum())
        - float(display_forecast["presented_low"].sum())
    ) / 2
    metrics = st.columns(3)
    metrics[0].metric("Projected sales", _money(total))
    metrics[1].metric(
        "Compared with prior forecast",
        _signed_money(total - previous_total) if previous_total is not None else "First forecast",
    )
    metrics[2].metric("Forecast range", f"± {_money(range_half_width)}")

    st.markdown('<div class="section-title">Current forecast</div>', unsafe_allow_html=True)
    if operational:
        st.markdown(
            '<div class="section-note">The dashed line is the marketing-clean organic baseline. The green line adds carryover from prior spend and expected lift from the implemented plan. The shaded band is the full scored uncertainty.</div>',
            unsafe_allow_html=True,
        )
        band = (
            alt.Chart(display_forecast)
            .mark_area(color=colors["band"], opacity=0.24)
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("presented_low:Q", title="Sales"),
                y2="presented_high:Q",
            )
        )
        organic_line = (
            alt.Chart(display_forecast)
            .mark_line(color=colors["baseline"], strokeDash=[6, 5], strokeWidth=2)
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
            .mark_line(color=colors["forecast"], strokeWidth=3, point=True)
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
            .mark_area(color=colors["band"], opacity=0.24)
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("presented_low:Q", title="Sales"),
                y2="presented_high:Q",
            )
        )
        forecast_line = (
            alt.Chart(display_forecast)
            .mark_line(color=colors["forecast"], strokeWidth=3)
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
    chart_observations = observations.tail(28) if not observations.empty else observations
    if not chart_observations.empty:
        actual_line = (
            alt.Chart(chart_observations)
            .mark_line(color=colors["actual"], strokeWidth=2)
            .encode(x="date:T", y="value:Q", tooltip=["date:T", "value:Q"])
        )
        chart = actual_line + chart
    st.altair_chart(chart.properties(height=390), use_container_width=True)

    plan_columns = st.columns(2, gap="medium")
    with plan_columns[0], st.container(border=True):
        st.markdown("### Marketing plan")
        if operational:
            planned = float(marketing_points["planned_lift_median"].sum())
            st.write(
                f"{_money(float(marketing.get('planned_spend') or 0.0))} planned spend · "
                f"{_signed_money(planned)} expected sales lift."
            )
            marketing_button = "Open plan"
        elif marketing is not None:
            lifecycle = str(marketing.get("lifecycle") or "learning").replace("_", " ")
            st.write(f"Marketing learning is {lifecycle}; no plan is scored yet.")
            marketing_button = "Review learning"
        else:
            st.write("No marketing plan is currently applied to this forecast.")
            marketing_button = "Create plan"
        if st.button(
            marketing_button,
            use_container_width=True,
            key=f"forecast_plan_summary_{forecast['site_id']}",
        ):
            if marketing is not None:
                st.session_state.mmm_plan_drawer_open = True
            else:
                st.session_state.show_marketing_workspace = True
            st.rerun()

    placements = list((event_plan or {}).get("placements") or [])
    with plan_columns[1], st.container(border=True):
        st.markdown("### Forecast events")
        if placements:
            total_event_effect = float(display_forecast["event_median"].sum())
            st.write(
                f"{len(placements)} known event occurrence(s) · "
                f"{_signed_money(total_event_effect)} expected effect."
            )
        else:
            st.write("No known future events are currently placed in this forecast.")
        if st.button(
            "Plan events",
            use_container_width=True,
            key=f"forecast_event_summary_{forecast['site_id']}",
        ):
            st.session_state.event_imputation_drawer_open = True
            st.rerun()

    st.markdown('<div class="section-title">Forecast replay</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Play through saved forecasts and compare each version with reported sales.</div>',
        unsafe_allow_html=True,
    )
    _render_forecast_replay(
        site_id=int(forecast["site_id"]),
        vintages=replay_vintages,
        points=replay_points,
        observations=data["observations"],
    )

    with st.expander("Run forecast again with the current approved data", expanded=False):
        st.caption(
            "This keeps the published forecast available while a new run uses the "
            "currently active workflow."
        )
        review_id = forecast.get("review_id")
        if st.button(
            "Run again with current data",
            type="primary",
            use_container_width=True,
            key=f"rerun_forecast_{forecast['run_id']}",
            disabled=review_id is None,
        ):
            try:
                scope = active_gateway_scope()
                if scope is not None:
                    new_run_id, _created = scope["client"].rerun_first_forecast(
                        org_id,
                        int(forecast["site_id"]),
                        int(forecast["metric_id"]),
                    )
                else:
                    new_run_id = _enqueue_fresh_first_forecast(int(review_id))
            except Exception as error:
                st.error(f"A new forecast could not be queued: {error}")
            else:
                st.toast(f"Forecast job #{new_run_id} was queued.", icon="✅")
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
