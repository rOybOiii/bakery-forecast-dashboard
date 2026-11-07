"""Manager-facing data updates that feed the Forecast Refresh workflow."""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st

from database_identity import Identity
from gateway_client import GatewayClient
from gateway_client import (
    GatewayOnboardingRepository,
    GatewayPipelineRepository,
    GatewayWorkflowController,
    active_gateway_scope,
)


ROOT = Path(__file__).resolve().parent


def _repository():
    if active_gateway_scope() is not None:
        return GatewayOnboardingRepository()
    try:
        from credent_onboarding import OnboardingRepository
    except ImportError:
        return None
    return OnboardingRepository(artifact_dir=ROOT.parent / ".credent-artifacts")


def _pipeline_repository():
    if active_gateway_scope() is not None:
        return GatewayPipelineRepository()
    try:
        from credent_orchestrator import PipelineRepository
    except ImportError:
        local_source = ROOT.parent / "Credent Core" / "packages" / "orchestrator" / "src"
        if local_source.exists() and str(local_source) not in sys.path:
            sys.path.insert(0, str(local_source))
        try:
            from credent_orchestrator import PipelineRepository
        except ImportError:
            return None
    return PipelineRepository()


def _workflow_controller(pipeline):
    if isinstance(pipeline, GatewayPipelineRepository):
        return GatewayWorkflowController()
    try:
        from credent_orchestrator import WorkflowController
    except ImportError:
        return None
    return WorkflowController(pipeline)


def _read_upload(uploaded) -> pd.DataFrame:
    payload = uploaded.getvalue()
    suffix = Path(uploaded.name).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(BytesIO(payload))
    if suffix in {".xlsx", ".xlsm"}:
        return pd.read_excel(BytesIO(payload))
    raise ValueError("Please provide a CSV or Excel workbook.")


def _entry_frame(site_id: int) -> pd.DataFrame:
    key = f"forecast_refresh_entry_{site_id}"
    if key not in st.session_state:
        days = [date.today() - timedelta(days=offset) for offset in range(6, -1, -1)]
        st.session_state[key] = pd.DataFrame({"Date": days, "Sales": [None] * 7})
    return st.data_editor(
        st.session_state[key],
        key=f"editor_{key}",
        hide_index=True,
        num_rows="dynamic",
        width="stretch",
        column_config={
            "Date": st.column_config.DateColumn("Date", required=True),
            "Sales": st.column_config.NumberColumn(
                "Sales", min_value=0.0, format="$%.2f"
            ),
        },
    )


def _api_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert editor/upload rows into JSON-safe daily observations."""

    safe = frame.copy()
    date_column = "Date" if "Date" in safe.columns else "date"
    if date_column in safe.columns:
        safe[date_column] = pd.to_datetime(safe[date_column]).dt.date.astype(str)
    safe = safe.astype(object).where(pd.notna(safe), None)
    return [dict(row) for row in safe.to_dict(orient="records")]


def render_forecast_refresh_controls(
    site: dict[str, Any],
    *,
    standalone: bool = False,
    actor_id: int | None = None,
    identity: Identity | None = None,
) -> None:
    """Preview, approve, and queue a small sales-data refresh."""

    site_id = int(site["site_id"])
    metric_id = int(site["metric_id"])
    surface = st.container(border=True) if standalone else st.expander(
        "Update sales data", expanded=False
    )
    with surface:
        if standalone:
            st.markdown("### Add or correct sales")
        st.write(
            "Enter a few days directly or upload a file. Credent will show what is "
            "new, late, changed, or already current before anything is saved."
        )
        mode = st.radio(
            "Input method",
            ("Enter or paste days", "Upload CSV or Excel"),
            horizontal=True,
            key=f"forecast_refresh_mode_{site_id}",
            label_visibility="collapsed",
        )
        frame: pd.DataFrame | None = None
        filename: str | None = None
        payload: bytes | None = None
        if mode == "Enter or paste days":
            edited = _entry_frame(site_id)
            if "Sales" in edited:
                frame = edited.loc[edited["Sales"].notna()].copy()
            filename = f"site-{site_id}-manual-sales-update.csv"
            if frame is not None and not frame.empty:
                payload = frame.to_csv(index=False).encode("utf-8")
        else:
            uploaded = st.file_uploader(
                "Sales update",
                type=["csv", "xlsx", "xlsm"],
                key=f"forecast_refresh_upload_{site_id}",
            )
            if uploaded is not None:
                try:
                    frame = _read_upload(uploaded)
                    filename = uploaded.name
                    payload = uploaded.getvalue()
                except Exception as error:
                    st.error(f"We couldn’t read that file: {error}")

        if frame is None or frame.empty or filename is None or payload is None:
            st.caption("Add at least one Date and Sales value to continue.")
            return

        gateway = (
            GatewayClient(str(identity.gateway_url), str(identity.gateway_token))
            if identity is not None and identity.gateway_url and identity.gateway_token
            else None
        )
        repository = None if gateway is not None else _repository()
        if repository is None and gateway is None:
            st.error("The data service is unavailable.")
            return
        try:
            preview = (
                gateway.preview_sales(
                    int(site["org_id"]), site_id, metric_id, _api_rows(frame)
                )
                if gateway is not None
                else repository.preview_observation_update(
                    site_id=site_id,
                    metric_id=metric_id,
                    rows=frame,
                )
            )
        except Exception as error:
            st.error(f"This update isn’t ready: {error}")
            if repository is not None:
                repository.close()
            return

        counts = preview["counts"]
        columns = st.columns(4)
        columns[0].metric("New days", counts["appended"])
        columns[1].metric("Late days", counts["late"])
        columns[2].metric("Corrections", counts["corrected"])
        columns[3].metric("Already current", counts["unchanged"])
        detail = pd.DataFrame(preview["details"]).rename(
            columns={
                "date": "Date",
                "previous_value": "Previous",
                "new_value": "Submitted",
                "classification": "Result",
            }
        )
        st.dataframe(detail, hide_index=True, width="stretch")

        correction = bool(counts["corrected"])
        if correction:
            st.warning(
                "This changes previously approved sales. Results derived from the "
                "older snapshot will be invalidated when you approve the update."
            )
        reason = st.text_input(
            "Reason for changing approved data" if correction else "Update note (optional)",
            key=f"forecast_refresh_reason_{site_id}",
            placeholder=(
                "Example: corrected a register export"
                if correction
                else "Example: entered the last three closing days"
            ),
        )
        disabled = preview["changed_rows"] == 0 or (correction and not reason.strip())
        if st.button(
            "Approve data and refresh forecast",
            type="primary",
            use_container_width=True,
            disabled=disabled,
            key=f"approve_forecast_refresh_{site_id}",
        ):
            pipeline = None
            run_id = None
            try:
                if gateway is not None:
                    result = gateway.apply_sales(
                        int(site["org_id"]),
                        site_id,
                        metric_id,
                        rows=_api_rows(frame),
                        filename=filename,
                        payload=payload,
                        reason=reason,
                    )
                    run_id = int(result["run_id"])
                    if result.get("recovery_id") is not None:
                        st.session_state.provenance_recovery_notice = int(
                            result["recovery_id"]
                        )
                else:
                    pipeline = _pipeline_repository()
                    if pipeline is None:
                        raise RuntimeError("The local forecast service is unavailable.")
                    update = repository.apply_observation_update(
                        site_id=site_id,
                        metric_id=metric_id,
                        rows=frame,
                        filename=filename,
                        payload=payload,
                        reason=reason,
                        actor_id=actor_id,
                    )
                    controller = _workflow_controller(pipeline)
                    if controller is None:
                        raise RuntimeError("The workflow controller is unavailable.")
                    if update["load_kind"] == "correction":
                        run_id, _, recovery_id = (
                            controller.enqueue_active_provenance_change_recovery(
                                int(update["review_id"]),
                                prior_dataset_id=int(update["prior_dataset_id"]),
                                replacement_dataset_id=int(update["dataset_id"]),
                                reason=reason,
                                change_summary=dict(update["change_summary"]),
                                changed_rows=list(update["change_details"]),
                            )
                        )
                        st.session_state.provenance_recovery_notice = recovery_id
                    else:
                        run_id, _ = controller.enqueue_active_forecast_refresh(
                            int(update["review_id"]),
                            run_kind=str(update["load_kind"]),
                            change_summary=dict(update["change_summary"]),
                        )
                    repository.link_forecast_refresh(
                        int(update["review_id"]), run_id
                    )
            except Exception as error:
                st.error(f"The update could not be queued: {error}")
            finally:
                if pipeline is not None:
                    pipeline.close()
                if repository is not None:
                    repository.close()
            if run_id is not None:
                st.session_state.completed_forecast_job = None
                recovery_id = st.session_state.pop(
                    "provenance_recovery_notice", None
                )
                if recovery_id is not None:
                    st.toast(
                        f"Provenance Change Recovery #{recovery_id} queued "
                        f"forecast job #{run_id}.",
                        icon="✅",
                    )
                else:
                    st.toast(f"Forecast refresh job #{run_id} was queued.", icon="✅")
                st.rerun()
        else:
            if repository is not None:
                repository.close()


__all__ = ["render_forecast_refresh_controls"]
