"""Site-scoped sales entry, correction, and recovery status."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import text

from database_event_families import _engine
from database_identity import Identity
from gateway_client import GatewayClient
from forecast_refresh_workspace import render_forecast_refresh_controls
from onboarding_workspace import render_run_details


_HISTORY_WINDOWS = {
    "7 days": 7,
    "30 days": 30,
    "90 days": 90,
    "180 days": 180,
    "1 year": 365,
}


def load_sales_data_workspace(
    *,
    site_id: int,
    metric_id: int,
    org_id: int | None = None,
    identity: Identity | None = None,
) -> dict[str, Any]:
    """Load current accepted observations and historical-correction recovery state."""

    if (
        identity is not None
        and identity.gateway_url
        and identity.gateway_token
        and org_id is not None
    ):
        return GatewayClient(
            identity.gateway_url, identity.gateway_token
        ).sales_workspace(org_id, site_id, metric_id)

    engine = _engine()
    if engine is None:
        return {"observations": [], "recoveries": []}
    try:
        with engine.connect() as connection:
            observations = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT current.date, current.value, current.dataset_id,
                            dataset.name AS dataset_name,
                            dataset.version AS dataset_version,
                            dataset.load_kind, dataset.created_at AS accepted_at
                        FROM current_observation current
                        JOIN dataset ON dataset.id = current.dataset_id
                        WHERE current.site_id = :site_id
                          AND current.metric_id = :metric_id
                        ORDER BY current.date
                        """
                    ),
                    {"site_id": site_id, "metric_id": metric_id},
                ).mappings()
            ]
            recoveries = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT recovery.id, recovery.reason, recovery.status,
                            recovery.anchor_run_id, recovery.anchor_dataset_id,
                            recovery.replacement_dataset_id,
                            recovery.recovery_run_id, recovery.manifest,
                            recovery.created_at, recovery.queued_at,
                            recovery.completed_at,
                            recovery_run.status AS recovery_run_status,
                            reconciliation.id AS reconciliation_dispatch_id,
                            reconciliation.status AS reconciliation_status,
                            reconciliation.reconciliation_run_id,
                            COALESCE(review.pending_count, 0) AS pending_event_matches,
                            COALESCE(review.auto_count, 0) AS auto_event_matches,
                            COALESCE(review.confirmed_count, 0) AS confirmed_event_matches
                        FROM provenance_change_recovery recovery
                        LEFT JOIN model_run recovery_run
                          ON recovery_run.id = recovery.recovery_run_id
                        LEFT JOIN event_reconciliation_dispatch reconciliation
                          ON reconciliation.recovery_id = recovery.id
                        LEFT JOIN LATERAL (
                            SELECT
                                COUNT(*) FILTER (
                                    WHERE link.decision = 'pending'
                                ) AS pending_count,
                                COUNT(*) FILTER (
                                    WHERE link.decision = 'auto_accepted'
                                ) AS auto_count,
                                COUNT(*) FILTER (
                                    WHERE link.decision = 'accepted'
                                ) AS confirmed_count
                            FROM event_reconciliation_link link
                            WHERE link.dispatch_id = reconciliation.id
                        ) review ON true
                        WHERE recovery.site_id = :site_id
                          AND recovery.metric_id = :metric_id
                        ORDER BY recovery.created_at DESC, recovery.id DESC
                        LIMIT 20
                        """
                    ),
                    {"site_id": site_id, "metric_id": metric_id},
                ).mappings()
            ]
    finally:
        engine.dispose()
    return {"observations": observations, "recoveries": recoveries}


def _recovery_stage(recovery: dict[str, Any]) -> tuple[str, str]:
    """Translate controller states into manager-readable recovery progress."""

    status = str(recovery.get("status") or recovery.get("recovery_run_status") or "queued")
    if status == "failed" or recovery.get("recovery_run_status") == "failed":
        return "Needs attention", "error"
    if status in {"prepared", "queued"}:
        return "Correction accepted · waiting to rebuild", "info"
    if status == "running" or recovery.get("recovery_run_status") == "running":
        return "Rebuilding the forecast", "info"
    reconciliation = recovery.get("reconciliation_status")
    if reconciliation in {None, "pending"}:
        return "Forecast rebuilt · preparing event recovery", "info"
    if reconciliation == "dispatched":
        return "Reconciling detected events", "info"
    if reconciliation == "failed":
        return "Event recovery needs attention", "error"
    if int(recovery.get("pending_event_matches") or 0):
        return "Forecast recovered · event confirmation needed", "warning"
    return "Recovery complete", "success"


def _filter_sales_history(
    frame: pd.DataFrame,
    *,
    history_window: str,
    custom_range: tuple[Any, Any] | list[Any] | None = None,
) -> pd.DataFrame:
    """Return the display window without changing the accepted data behind it."""

    if frame.empty:
        return frame.copy()
    dated = frame.copy()
    dated["date"] = pd.to_datetime(dated["date"]).dt.normalize()
    if history_window == "Custom":
        if not custom_range or len(custom_range) != 2:
            return dated.iloc[0:0]
        start, end = (pd.Timestamp(value).normalize() for value in custom_range)
        if end < start:
            start, end = end, start
    else:
        days = int(_HISTORY_WINDOWS.get(history_window, 30))
        end = dated["date"].max()
        start = end - timedelta(days=days - 1)
    return dated.loc[dated["date"].between(start, end)].copy()


def _render_current_sales(
    observations: list[dict[str, Any]],
    *,
    site_id: int,
) -> None:
    if not observations:
        st.info("No accepted sales observations are available for this site yet.")
        return
    frame = pd.DataFrame(observations)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["value"] = pd.to_numeric(frame["value"])
    frame = frame.sort_values("date").reset_index(drop=True)
    latest = frame.iloc[-1]
    history_window = st.selectbox(
        "History to show",
        ["7 days", "30 days", "90 days", "180 days", "1 year", "Custom"],
        index=1,
        key=f"sales_data_history_window_{site_id}",
    )
    custom_range = None
    if history_window == "Custom":
        latest_date = frame["date"].max().date()
        earliest_date = frame["date"].min().date()
        suggested_start = max(
            earliest_date,
            (frame["date"].max() - pd.Timedelta(days=29)).date(),
        )
        selected = st.date_input(
            "Date range",
            value=(suggested_start, latest_date),
            min_value=earliest_date,
            max_value=latest_date,
            key=f"sales_data_custom_history_range_{site_id}",
        )
        if isinstance(selected, (tuple, list)) and len(selected) == 2:
            custom_range = selected
        else:
            st.info("Choose an end date to show the custom history range.")
    visible = _filter_sales_history(
        frame,
        history_window=history_window,
        custom_range=custom_range,
    )
    metrics = st.columns(4)
    metrics[0].metric("Days shown", f"{len(visible):,}")
    metrics[1].metric("History begins", f"{frame['date'].min():%b %-d, %Y}")
    metrics[2].metric("Latest sales date", f"{frame['date'].max():%b %-d, %Y}")
    metrics[3].metric("Latest sales", f"${float(latest['value']):,.0f}")
    st.caption(
        f"Current source: {latest['dataset_name']} · version "
        f"{int(latest['dataset_version'])} · {str(latest['load_kind']).replace('_', ' ').title()}"
    )
    st.caption(
        f"Showing {len(visible):,} of {len(frame):,} accepted sales days. "
        "This display choice does not change correction or recovery checks."
    )
    if visible.empty:
        st.info("No accepted sales data falls within that range.")
        return
    chart = visible.rename(columns={"date": "Date", "value": "Sales"})
    st.line_chart(chart.set_index("Date")["Sales"], height=260)
    with st.expander("View sales rows in this period"):
        display = visible.sort_values("date", ascending=False).rename(
            columns={"date": "Date", "value": "Sales"}
        )
        st.dataframe(
            display[["Date", "Sales", "dataset_name", "dataset_version"]],
            hide_index=True,
            width="stretch",
            column_config={
                "dataset_name": "Source",
                "dataset_version": "Version",
                "Sales": st.column_config.NumberColumn(format="$%.2f"),
            },
        )


def _render_recovery_history(recoveries: list[dict[str, Any]]) -> None:
    st.markdown("### Corrections and recovery")
    st.caption(
        "When an accepted historical value changes, Credent rebuilds affected results "
        "without deleting the original record."
    )
    if not recoveries:
        st.info("No historical corrections have required recovery for this site.")
        return
    for recovery in recoveries:
        stage, tone = _recovery_stage(recovery)
        created = pd.Timestamp(recovery["created_at"])
        with st.expander(
            f"Correction #{int(recovery['id'])} · {created:%b %-d, %Y} · {stage}",
            expanded=tone in {"error", "warning", "info"}
            and stage != "Recovery complete",
        ):
            if tone == "error":
                st.error(stage)
            elif tone == "warning":
                st.warning(stage)
            elif tone == "success":
                st.success(stage)
            else:
                st.info(stage)
            st.write(f"**Reason:** {recovery['reason']}")
            manifest = dict(recovery.get("manifest") or {})
            changed_rows = list(manifest.get("changed_rows") or [])
            changed_dates = [
                str(item.get("date")) if isinstance(item, dict) else str(item)
                for item in changed_rows
            ]
            if changed_dates:
                preview = ", ".join(changed_dates[:8])
                if len(changed_dates) > 8:
                    preview += f" and {len(changed_dates) - 8} more"
                st.caption(f"Changed dates: {preview}")
            st.caption(
                f"Input anchor: forecast job #{int(recovery['anchor_run_id'])} · "
                f"source dataset #{int(recovery['anchor_dataset_id'])}"
            )
            event_summary = st.columns(3)
            event_summary[0].metric(
                "Auto-reconciled", int(recovery.get("auto_event_matches") or 0)
            )
            event_summary[1].metric(
                "Manager-confirmed", int(recovery.get("confirmed_event_matches") or 0)
            )
            event_summary[2].metric(
                "Needs confirmation", int(recovery.get("pending_event_matches") or 0)
            )
            if recovery.get("recovery_run_id") is not None:
                render_run_details(int(recovery["recovery_run_id"]))
            if recovery.get("reconciliation_run_id") is not None:
                render_run_details(int(recovery["reconciliation_run_id"]))


def render_sales_data_workspace(
    *,
    site: dict[str, Any],
    site_name: str,
    actor_id: int,
    identity: Identity | None = None,
) -> None:
    """Render daily sales operations for the currently selected site."""

    site_id = int(site["site_id"])
    metric_id = int(site["metric_id"])
    try:
        workspace = load_sales_data_workspace(
            site_id=site_id,
            metric_id=metric_id,
            org_id=int(site["org_id"]),
            identity=identity,
        )
    except Exception as error:
        st.error(f"Sales data is temporarily unavailable: {error}")
        return
    st.markdown('<div class="dashboard-kicker">Daily operations</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="dashboard-title">Sales data</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="dashboard-subtitle">{site_name} · Accepted history, updates, and corrections</div>',
        unsafe_allow_html=True,
    )
    current_tab, update_tab, recovery_tab = st.tabs(
        ["Current sales", "Add or correct sales", "Corrections & recovery"]
    )
    with current_tab:
        _render_current_sales(list(workspace["observations"]), site_id=site_id)
    with update_tab:
        render_forecast_refresh_controls(
            site,
            standalone=True,
            actor_id=actor_id,
            identity=identity,
        )
    with recovery_tab:
        _render_recovery_history(list(workspace["recoveries"]))


__all__ = [
    "_filter_sales_history",
    "load_sales_data_workspace",
    "render_sales_data_workspace",
]
