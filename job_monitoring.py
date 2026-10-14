"""Plain-language Streamlit monitoring for the local controller and workers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from onboarding_workspace import _pipeline_repository


_STAGE_LABELS = {
    "preflight": "Checking the modeling environment",
    "detrend": "Estimating the baseline",
    "pprm_batch": "Finding persistent sales regimes",
    "jeam_pass1": "Finding cross-site event support",
    "jeam_pass2": "Estimating event effects",
    "erp": "Building recurring-event patterns",
    "forecast": "Building the forecast",
    "validate_first_forecast": "Checking the forecast",
}


def _duration(seconds: object) -> str:
    total = max(0, int(float(seconds or 0)))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _time_label(value: datetime | None) -> str:
    if value is None:
        return "—"
    return pd.Timestamp(value).strftime("%b %-d, %-I:%M %p")


def _status_label(status: object) -> str:
    return str(status or "unknown").replace("_", " ").title()


@st.fragment(run_every="2s")
def render_job_monitoring(org_id: int) -> None:
    """Refresh queue and worker state without rerunning the rest of the app."""

    pipeline = _pipeline_repository()
    if pipeline is None:
        st.error("The local pipeline service is unavailable.")
        return
    try:
        snapshot = pipeline.monitoring_snapshot(org_id=org_id)
    except Exception as error:
        st.error(f"Job monitoring is temporarily unavailable: {error}")
        return
    finally:
        pipeline.close()

    summary = dict(snapshot["summary"])
    workers = list(snapshot["workers"])
    jobs = list(snapshot["jobs"])
    healthy_workers = sum(worker["health"] == "healthy" for worker in workers)
    attention = int(summary["failed_jobs"]) + int(summary["expired_leases"])

    st.markdown('<div class="dashboard-kicker">Operations</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="dashboard-title">Jobs & workers</h1>', unsafe_allow_html=True)
    st.caption("Live controller state from the job database. This page refreshes automatically.")

    columns = st.columns(4)
    columns[0].metric("Healthy workers", f"{healthy_workers} of {len(workers)}")
    columns[1].metric("Waiting jobs", int(summary["queued_jobs"]))
    columns[2].metric("Running jobs", int(summary["running_jobs"]))
    columns[3].metric("Needs attention", attention)
    if summary["oldest_queue_seconds"]:
        st.caption(
            f"The oldest waiting job has been queued for "
            f"{_duration(summary['oldest_queue_seconds'])}."
        )

    st.markdown("## Workers")
    if not workers:
        st.warning("No workers have registered yet.")
    for worker in workers:
        health = str(worker["health"])
        with st.container(border=True):
            name, state, capacity = st.columns([2, 1, 2])
            name.markdown(f"**{worker['name']}**")
            if health == "healthy":
                state.success("Healthy")
            elif health == "draining":
                state.warning("Draining")
            else:
                state.error(_status_label(health))
            capacity.caption(
                f"CPU {int(worker['used_cpu_slots'])}/{int(worker['cpu_slots'])} · "
                f"Memory {int(worker['used_memory_mb']):,}/{int(worker['memory_mb']):,} MB"
            )
            age = worker.get("heartbeat_age_seconds")
            st.caption(
                "Checking in now"
                if age is not None and float(age) < 5
                else f"Last check-in {_duration(age)} ago"
            )

    st.markdown("## Recent jobs")
    if not jobs:
        st.info("No jobs have been submitted for this organization yet.")
        return
    rows: list[dict[str, Any]] = []
    for job in jobs:
        total = int(job["total_steps"] or 0)
        complete = int(job["completed_steps"] or 0)
        current = job.get("current_stage")
        rows.append(
            {
                "Job": int(job["id"]),
                "Site": job.get("site_name") or "—",
                "Status": _status_label(job["status"]),
                "Progress": f"{complete}/{total}",
                "Current work": _STAGE_LABELS.get(str(current), _status_label(current))
                if current
                else "—",
                "Workflow": (
                    f"{job['workflow_name']} {job['workflow_version']}"
                    if job.get("workflow_name")
                    else "Legacy"
                ),
                "Requested": _time_label(job.get("requested_at")),
                "Elapsed": _duration(job.get("elapsed_seconds")),
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={"Job": st.column_config.NumberColumn(format="#%d")},
    )


__all__ = ["render_job_monitoring"]
