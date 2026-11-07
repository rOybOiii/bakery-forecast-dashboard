"""Database-backed manager controls for site event families."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text
from gateway_client import (
    GatewayPipelineRepository,
    GatewayWorkflowController,
    active_gateway_scope,
    application_call,
)


ROOT = Path(__file__).resolve().parent


def _pipeline_services():
    try:
        from credent_orchestrator import PipelineRepository, WorkflowController
    except ImportError:
        local_source = ROOT.parent / "Credent Core" / "packages" / "orchestrator" / "src"
        if local_source.exists() and str(local_source) not in sys.path:
            sys.path.insert(0, str(local_source))
        try:
            from credent_orchestrator import PipelineRepository, WorkflowController
        except ImportError:
            return None
    return PipelineRepository, WorkflowController


def _engine():
    try:
        from credent_db import make_engine
    except ImportError:
        return None
    return make_engine()


def load_site_family_workspace(site_id: int) -> dict[str, Any]:
    """Load current detections, manager membership, estimates, and family jobs."""

    if active_gateway_scope() is not None:
        return application_call("workspace", "events.load_family_workspace", site_id)
    engine = _engine()
    if engine is None:
        return {"events": [], "families": []}
    try:
        with engine.connect() as connection:
            current_run_id = connection.execute(
                text(
                    """
                    SELECT a.run_id
                    FROM current_forecast f
                    JOIN artifact a ON a.id = f.artifact_id
                    WHERE f.site_id = :site_id
                    ORDER BY f.published_at DESC, a.run_id DESC
                    LIMIT 1
                    """
                ),
                {"site_id": site_id},
            ).scalar_one_or_none()
            events = []
            if current_run_id is not None:
                events = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            """
                            SELECT e.id, e.start_date, e.end_date, e.support,
                                e.direction, e.status, e.detection_key,
                                ec.family_id, f.name AS family_name,
                                c.name AS category_name
                            FROM current_event e
                            JOIN event_inference_lineage lineage
                              ON lineage.event_id = e.id
                            LEFT JOIN event_class ec
                              ON ec.event_id = e.id AND ec.end_at IS NULL
                            LEFT JOIN family f ON f.id = ec.family_id
                            LEFT JOIN category c ON c.id = ec.category_id
                            WHERE e.run_id = :run_id AND e.site_id = :site_id
                            ORDER BY e.start_date DESC, e.id DESC
                            """
                        ),
                        {"run_id": current_run_id, "site_id": site_id},
                    ).mappings()
                ]
            families = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT f.id, f.name, f.version, c.name AS category_name,
                            COALESCE(members.member_count, 0) AS member_count,
                            fe.id AS estimate_id, fe.run_id AS estimate_run_id,
                            fe.unit, fe.quality, fe.direction, fe.stability,
                            fe.statistics, fe.version AS estimate_version,
                            fe.membership_hash, fe.created_at,
                            latest_job.id AS job_id, latest_job.status AS job_status
                        FROM family f
                        JOIN category c ON c.id = f.category_id
                        LEFT JOIN LATERAL (
                            SELECT COUNT(*) AS member_count
                            FROM event_class ec
                            WHERE ec.family_id = f.id AND ec.end_at IS NULL
                        ) members ON true
                        LEFT JOIN current_family_estimate fe
                          ON fe.family_id = f.id AND fe.site_id = f.site_id
                        LEFT JOIN LATERAL (
                            SELECT mr.id, mr.status
                            FROM model_run mr
                            WHERE mr.run_kind = 'family_refresh'
                              AND mr.profile_snapshot #>>
                                  '{workflow_inputs,family_snapshot,family_id}' = f.id::text
                            ORDER BY mr.requested_at DESC, mr.id DESC
                            LIMIT 1
                        ) latest_job ON true
                        WHERE f.site_id = :site_id AND f.status = 'active'
                        ORDER BY f.name
                        """
                    ),
                    {"site_id": site_id},
                ).mappings()
            ]
    finally:
        engine.dispose()
    return {
        "current_run_id": int(current_run_id) if current_run_id is not None else None,
        "events": events,
        "families": families,
    }


def load_family_shape(estimate_id: int) -> list[dict[str, Any]]:
    if active_gateway_scope() is not None:
        return application_call("workspace", "events.load_family_shape", estimate_id)
    engine = _engine()
    if engine is None:
        return []
    try:
        with engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT day, low, median, high
                        FROM family_shape
                        WHERE estimate_id = :estimate_id
                        ORDER BY day
                        """
                    ),
                    {"estimate_id": estimate_id},
                ).mappings()
            ]
    finally:
        engine.dispose()


def assign_and_refresh_family(
    *,
    org_id: int,
    site_id: int,
    event_id: int,
    actor_id: int,
    family_id: int | None = None,
    family_name: str | None = None,
    category_name: str | None = None,
) -> tuple[int, int, bool]:
    """Persist one manager assignment and queue the active family workflow."""

    if active_gateway_scope() is not None:
        result = application_call(
            "workspace",
            "events.assign_refresh_family",
            org_id=org_id,
            site_id=site_id,
            event_id=event_id,
            actor_id=actor_id,
            family_id=family_id,
            family_name=family_name,
            category_name=category_name,
        )
        return tuple(result)
    services = _pipeline_services()
    if services is None:
        raise RuntimeError("The local pipeline service is unavailable.")
    repository_class, controller_class = services
    repository = repository_class()
    try:
        if family_id is None:
            if not family_name or not category_name:
                raise ValueError("A new family needs both a name and a category.")
            family_id = repository.create_site_family(
                site_id=site_id,
                name=family_name,
                category_name=category_name,
            )
        changed = repository.assign_event_to_site_family(
            event_id=event_id,
            family_id=family_id,
            assigned_by=actor_id,
        )
        controller = controller_class(repository)
        run_id, coalesced = controller.enqueue_active_site_family_refresh(
            family_id,
            requested_by=actor_id,
        )
        return int(family_id), int(run_id), bool(coalesced and not changed)
    finally:
        repository.close()


def refresh_family(*, org_id: int, family_id: int, actor_id: int) -> tuple[int, bool]:
    if active_gateway_scope() is not None:
        result = application_call(
            "workspace",
            "events.refresh_family",
            org_id=org_id,
            family_id=family_id,
            actor_id=actor_id,
        )
        return tuple(result)
    services = _pipeline_services()
    if services is None:
        raise RuntimeError("The local pipeline service is unavailable.")
    repository_class, controller_class = services
    repository = repository_class()
    try:
        controller = controller_class(repository)
        run_id, coalesced = controller.enqueue_active_site_family_refresh(
            family_id,
            requested_by=actor_id,
        )
        return int(run_id), bool(coalesced)
    finally:
        repository.close()


@st.fragment(run_every="2s")
def _render_family_job(run_id: int) -> None:
    if active_gateway_scope() is not None:
        repository = GatewayPipelineRepository()
        try:
            queue = repository.queue_status(run_id)
        except Exception as error:
            st.warning(f"Family job #{run_id} status is temporarily unavailable: {error}")
            return
        finally:
            repository.close()
        services = None
    else:
        services = _pipeline_services()
        if services is None:
            return
        repository_class, _ = services
        repository = repository_class()
        try:
            queue = repository.queue_status(run_id)
        except Exception as error:
            st.warning(f"Family job #{run_id} status is temporarily unavailable: {error}")
            return
        finally:
            repository.close()
    status = str(queue["status"])
    completed = int(queue.get("completed_steps") or 0)
    total = int(queue.get("total_steps") or 0)
    if total:
        st.progress(
            min(completed / total, 1.0),
            text=f"Family job #{run_id} · {completed} of {total} stages complete",
        )
    if status == "queued":
        st.info(f"Family job #{run_id} is queued.")
    elif status == "running":
        st.info(f"Family job #{run_id} is building the family statistics.")
    elif status == "published":
        st.success(f"Family job #{run_id} published the current statistics.")
    elif status == "failed":
        st.error(f"Family job #{run_id} failed. Open Jobs & workers for details.")
    elif status == "cancelled":
        st.warning(f"Family job #{run_id} was replaced after membership changed.")


def _event_label(event: dict[str, Any]) -> str:
    start = pd.Timestamp(event["start_date"])
    end = pd.Timestamp(event["end_date"])
    dates = f"{start:%b %-d, %Y}" if start == end else f"{start:%b %-d}–{end:%b %-d, %Y}"
    family = str(event.get("family_name") or "Unassigned")
    return f"{dates} · {str(event['direction']).title()} · {family}"


def _family_shape_chart(rows: list[dict[str, Any]]) -> alt.Chart:
    frame = pd.DataFrame(rows)
    for name in ("low", "median", "high"):
        frame[name] = pd.to_numeric(frame[name])
    frame["Event day"] = frame["day"].astype(int) + 1
    band = alt.Chart(frame).mark_area(color="#8fb9a8", opacity=0.3).encode(
        x=alt.X("Event day:Q", title="Day within the event"),
        y=alt.Y("low:Q", title="Modeled event effect"),
        y2="high:Q",
    )
    line = alt.Chart(frame).mark_line(color="#2f6b55", strokeWidth=3).encode(
        x="Event day:Q",
        y="median:Q",
        tooltip=["Event day:Q", "low:Q", "median:Q", "high:Q"],
    )
    return (band + line).properties(height=260)


def render_site_family_workspace(*, org_id: int, site_id: int, actor_id: int) -> None:
    """Render manager-driven family assignment and current pooled statistics."""

    try:
        workspace = load_site_family_workspace(site_id)
    except Exception as error:
        st.warning(f"Event families are temporarily unavailable: {error}")
        return
    events = list(workspace["events"])
    families = list(workspace["families"])

    st.markdown('<div class="section-title">Event families</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Managers decide which detected occurrences belong together. '
        'Credent then rebuilds the site-level family shape and statistics from their saved event evidence.</div>',
        unsafe_allow_html=True,
    )
    active_run = st.session_state.get("site_family_refresh_run_id")
    if active_run is not None:
        _render_family_job(int(active_run))

    if not events:
        st.info("The current forecast has no detected events to classify.")
        return

    assignment_tab, statistics_tab = st.tabs(["Assign occurrences", "Family statistics"])
    with assignment_tab:
        event_by_id = {int(item["id"]): item for item in events}
        selected_event_id = st.selectbox(
            "Detected occurrence",
            options=list(event_by_id),
            format_func=lambda value: _event_label(event_by_id[value]),
            key="site_family_event_picker",
        )
        selected_event = event_by_id[int(selected_event_id)]
        if selected_event.get("family_name"):
            st.caption(
                f"Currently assigned to **{selected_event['family_name']}** in "
                f"**{selected_event['category_name']}**. Saving another choice creates a reviewed revision."
            )

        modes = ["Existing family", "New family"] if families else ["New family"]
        mode = st.radio(
            "Family choice",
            modes,
            horizontal=True,
            key="site_family_assignment_mode",
        )
        selected_family_id: int | None = None
        family_name = None
        category_name = None
        if mode == "Existing family":
            family_by_id = {int(item["id"]): item for item in families}
            default_id = selected_event.get("family_id")
            options = list(family_by_id)
            index = options.index(int(default_id)) if default_id in options else 0
            selected_family_id = int(
                st.selectbox(
                    "Family",
                    options=options,
                    index=index,
                    format_func=lambda value: (
                        f"{family_by_id[value]['name']} · {family_by_id[value]['category_name']}"
                    ),
                    key="site_family_existing_picker",
                )
            )
        else:
            columns = st.columns(2)
            family_name = columns[0].text_input(
                "New family name",
                placeholder="Example: Payday weekend",
                key="site_family_new_name",
            )
            category_name = columns[1].text_input(
                "Category",
                placeholder="Example: Calendar",
                key="site_family_new_category",
            )
        disabled = mode == "New family" and (
            not str(family_name or "").strip() or not str(category_name or "").strip()
        )
        if st.button(
            "Save assignment and refresh statistics",
            type="primary",
            disabled=disabled,
            use_container_width=True,
            key="save_site_family_assignment",
        ):
            try:
                assigned_family_id, run_id, reused = assign_and_refresh_family(
                    org_id=org_id,
                    site_id=site_id,
                    event_id=int(selected_event_id),
                    actor_id=actor_id,
                    family_id=selected_family_id,
                    family_name=str(family_name or "").strip() or None,
                    category_name=str(category_name or "").strip() or None,
                )
            except Exception as error:
                st.error(f"The family assignment could not be saved: {error}")
            else:
                st.session_state.site_family_refresh_run_id = run_id
                st.session_state.site_family_selected_id = assigned_family_id
                st.toast(
                    f"Family job #{run_id} is already active."
                    if reused
                    else f"Family job #{run_id} was queued.",
                    icon="✅",
                )
                st.rerun()

    with statistics_tab:
        if not families:
            st.info("Assign an occurrence to create the first site family.")
            return
        family_by_id = {int(item["id"]): item for item in families}
        preferred = int(
            st.session_state.get("site_family_selected_id") or next(iter(family_by_id))
        )
        if preferred not in family_by_id:
            preferred = next(iter(family_by_id))
        selected_family_id = int(
            st.selectbox(
                "Family",
                options=list(family_by_id),
                index=list(family_by_id).index(preferred),
                format_func=lambda value: str(family_by_id[value]["name"]),
                key="site_family_statistics_picker",
            )
        )
        family = family_by_id[selected_family_id]
        header, action = st.columns([3, 1])
        header.markdown(f"### {family['name']}")
        header.caption(
            f"{family['category_name']} · {int(family['member_count'] or 0)} manager-linked occurrence(s)"
        )
        if action.button(
            "Refresh statistics",
            use_container_width=True,
            disabled=int(family["member_count"] or 0) == 0,
            key=f"refresh_site_family_{selected_family_id}",
        ):
            try:
                run_id, reused = refresh_family(
                    org_id=org_id,
                    family_id=selected_family_id,
                    actor_id=actor_id,
                )
            except Exception as error:
                st.error(f"The family refresh could not be queued: {error}")
            else:
                st.session_state.site_family_refresh_run_id = run_id
                st.toast(
                    f"Family job #{run_id} is already active."
                    if reused
                    else f"Family job #{run_id} was queued.",
                    icon="✅",
                )
                st.rerun()

        if family.get("estimate_id") is None:
            status = str(family.get("job_status") or "not started").replace("_", " ").title()
            st.info(f"No current family estimate yet. Latest job: {status}.")
            return
        statistics = dict(family.get("statistics") or {})
        metrics = st.columns(4)
        metrics[0].metric("Occurrences used", int(family.get("member_count") or 0))
        metrics[1].metric(
            "Effect direction",
            str(statistics.get("effect_direction") or "Unknown").title(),
        )
        metrics[2].metric("Trend", str(family.get("direction") or "Unknown").title())
        metrics[3].metric("Stability", str(family.get("stability") or "Unknown").title())
        probability = statistics.get("positive_probability")
        if probability is not None:
            st.caption(
                f"Probability of a positive effect: {float(probability):.0%} · "
                f"Evidence quality: {str(family.get('quality') or 'unknown').title()} · "
                f"Estimate version {int(family['estimate_version'])}"
            )
        shape = load_family_shape(int(family["estimate_id"]))
        if shape:
            st.altair_chart(_family_shape_chart(shape), use_container_width=True)
            st.caption(
                "The line is the pooled family shape; the band carries within-event and "
                "between-event uncertainty from the linked ERP morphology."
            )


__all__ = [
    "assign_and_refresh_family",
    "load_family_shape",
    "load_site_family_workspace",
    "refresh_family",
    "render_site_family_workspace",
]
