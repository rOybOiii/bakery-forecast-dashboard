"""Database-backed manager workspace for reviewing detected events."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text
from gateway_client import active_gateway_scope, application_call

from database_event_families import (
    _engine,
    refresh_family,
    render_site_family_workspace,
)
from database_event_reconciliation import (
    DECISION_LABELS,
    load_event_reconciliations,
    save_reconciliation_decision,
)


REVIEW_STATES = {
    "Needs review": None,
    "Confirmed": "confirmed",
    "Unresolved": "uncertain",
    "Dismissed": "incorrect",
}
STATE_LABELS = {
    None: "Needs review",
    "confirmed": "Confirmed",
    "uncertain": "Unresolved",
    "incorrect": "Dismissed",
}


def _clean_tags(value: str) -> list[str]:
    """Normalize a comma-separated manager tag list without losing order."""

    tags: list[str] = []
    for raw in value.split(","):
        clean = "-".join(raw.strip().lower().split())
        if clean and clean not in tags:
            tags.append(clean)
    return tags


def load_events_workspace(site_id: int) -> dict[str, Any]:
    if active_gateway_scope() is not None:
        return application_call("workspace", "events.load_workspace", site_id)
    """Load current detections and their latest durable manager interpretations."""

    engine = _engine()
    if engine is None:
        return {"run_id": None, "events": [], "categories": [], "families": [], "tags": [], "reconciliations": []}
    try:
        with engine.connect() as connection:
            run = connection.execute(
                text(
                    """
                    SELECT a.run_id, r.input_artifact_id, f.metric_id
                    FROM current_forecast f
                    JOIN artifact a ON a.id = f.artifact_id
                    JOIN model_run r ON r.id = a.run_id
                    WHERE f.site_id = :site_id
                    ORDER BY f.published_at DESC, a.run_id DESC
                    LIMIT 1
                    """
                ),
                {"site_id": site_id},
            ).mappings().one_or_none()
            if run is None:
                return {"run_id": None, "events": [], "categories": [], "families": [], "tags": [], "reconciliations": []}
            events = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT e.id, e.start_date, e.end_date, e.support,
                            e.direction, e.unit, e.source, e.status,
                            lineage.erp_island_id,
                            lineage.gate_model_version,
                            review.id AS review_id, review.title, review.note,
                            review.state AS review_state, review.created_at AS reviewed_at,
                            ec.category_id, c.name AS category_name,
                            ec.family_id, family.name AS family_name,
                            COALESCE(review_tags.tags, ARRAY[]::TEXT[]) AS tags
                        FROM current_event e
                        JOIN event_inference_lineage lineage
                          ON lineage.event_id = e.id
                        LEFT JOIN LATERAL (
                            SELECT er.*
                            FROM event_review er
                            WHERE er.event_id = e.id
                              AND NOT EXISTS (
                                  SELECT 1 FROM event_reconciliation_link inactive
                                  WHERE inactive.carried_review_id = er.id
                                    AND inactive.decision IN ('pending', 'rejected')
                              )
                            ORDER BY er.created_at DESC, er.id DESC
                            LIMIT 1
                        ) review ON true
                        LEFT JOIN LATERAL (
                            SELECT array_agg(t.name ORDER BY t.name) AS tags
                            FROM event_tag et
                            JOIN tag t ON t.id = et.tag_id
                            WHERE et.review_id = review.id
                        ) review_tags ON true
                        LEFT JOIN event_class ec
                          ON ec.event_id = e.id AND ec.end_at IS NULL
                        LEFT JOIN category c ON c.id = ec.category_id
                        LEFT JOIN family ON family.id = ec.family_id
                        WHERE e.run_id = :run_id AND e.site_id = :site_id
                        ORDER BY e.start_date DESC, e.id DESC
                        """
                    ),
                    {"run_id": int(run["run_id"]), "site_id": site_id},
                ).mappings()
            ]
            site = connection.execute(
                text("SELECT org_id FROM site WHERE id = :site_id"),
                {"site_id": site_id},
            ).mappings().one()
            categories = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id, name
                        FROM category
                        WHERE org_id = :org_id AND status = 'active'
                        ORDER BY CASE WHEN system_key = 'unidentified' THEN 0 ELSE 1 END, name
                        """
                    ),
                    {"org_id": int(site["org_id"])},
                ).mappings()
            ]
            families = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT f.id, f.name, f.category_id, c.name AS category_name
                        FROM family f
                        JOIN category c ON c.id = f.category_id
                        WHERE f.org_id = :org_id AND f.site_id = :site_id
                          AND f.status = 'active'
                        ORDER BY f.name
                        """
                    ),
                    {"org_id": int(site["org_id"]), "site_id": site_id},
                ).mappings()
            ]
            tags = [
                str(value)
                for value in connection.execute(
                    text(
                        """
                        SELECT name FROM tag
                        WHERE org_id = :org_id AND status = 'active'
                        ORDER BY name
                        """
                    ),
                    {"org_id": int(site["org_id"])},
                ).scalars()
            ]
            observations = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT o.date, o.value
                        FROM observation o
                        JOIN artifact input_artifact
                          ON input_artifact.id = :input_artifact_id
                        JOIN data_review dr
                          ON dr.id = input_artifact.review_id
                         AND dr.dataset_id = o.dataset_id
                        WHERE o.site_id = :site_id AND dr.metric_id = :metric_id
                        ORDER BY o.date
                        """
                    ),
                    {
                        "site_id": site_id,
                        "metric_id": int(run["metric_id"]),
                        "input_artifact_id": int(run["input_artifact_id"]),
                    },
                ).mappings()
            ]
    finally:
        engine.dispose()
    return {
        "run_id": int(run["run_id"]),
        "events": events,
        "categories": categories,
        "families": families,
        "tags": tags,
        "observations": observations,
        "reconciliations": load_event_reconciliations(site_id),
    }


def save_event_review(
    *,
    org_id: int,
    site_id: int,
    event_id: int,
    title: str,
    note: str,
    state: str | None,
    tags: list[str],
    category_id: int,
    family_id: int | None,
    actor_id: int,
) -> dict[str, Any]:
    """Append a manager revision and replace the current classification atomically."""

    if active_gateway_scope() is not None:
        return application_call(
            "workspace",
            "events.save_review",
            org_id=org_id,
            site_id=site_id,
            event_id=event_id,
            title=title,
            note=note,
            state=state,
            tags=tags,
            category_id=category_id,
            family_id=family_id,
            actor_id=actor_id,
        )
    if state not in {"confirmed", "uncertain", "incorrect"}:
        raise ValueError("Choose a review status before saving.")
    clean_title = " ".join(title.split())
    if not clean_title:
        raise ValueError("Give this occurrence a name before saving.")
    if state == "incorrect":
        # A dismissed occurrence stays visible as historical manager evidence,
        # but it must not remain usable as a family member.
        family_id = None
    engine = _engine()
    if engine is None:
        raise RuntimeError("The local database service is unavailable.")
    old_family_id: int | None = None
    changed_family = False
    try:
        with engine.begin() as connection:
            event = connection.execute(
                text(
                    """
                    SELECT e.id, e.site_id, s.org_id
                    FROM current_event e
                    JOIN event_inference_lineage lineage
                      ON lineage.event_id = e.id
                    JOIN site s ON s.id = e.site_id
                    WHERE e.id = :event_id AND e.site_id = :site_id
                    FOR UPDATE OF e
                    """
                ),
                {"event_id": event_id, "site_id": site_id},
            ).mappings().one_or_none()
            if event is None or int(event["org_id"]) != org_id:
                raise ValueError("That occurrence is no longer current for this site.")
            if family_id is not None:
                family = connection.execute(
                    text(
                        """
                        SELECT id, category_id FROM family
                        WHERE id = :family_id AND site_id = :site_id AND status = 'active'
                        """
                    ),
                    {"family_id": family_id, "site_id": site_id},
                ).mappings().one_or_none()
                if family is None:
                    raise ValueError("That family is not available for this site.")
                category_id = int(family["category_id"])
            else:
                valid_category = connection.execute(
                    text(
                        """
                        SELECT id FROM category
                        WHERE id = :category_id AND org_id = :org_id AND status = 'active'
                        """
                    ),
                    {"category_id": category_id, "org_id": org_id},
                ).scalar_one_or_none()
                if valid_category is None:
                    raise ValueError("That category is not available for this organization.")

            parent_id = connection.execute(
                text(
                    """
                    SELECT id FROM event_review
                    WHERE event_id = :event_id
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """
                ),
                {"event_id": event_id},
            ).scalar_one_or_none()
            review_id = int(
                connection.execute(
                    text(
                        """
                        INSERT INTO event_review (
                            event_id, parent_id, created_by, title, note, state
                        ) VALUES (
                            :event_id, :parent_id, :created_by, :title, :note, :state
                        ) RETURNING id
                        """
                    ),
                    {
                        "event_id": event_id,
                        "parent_id": parent_id,
                        "created_by": actor_id,
                        "title": clean_title,
                        "note": note.strip() or None,
                        "state": state,
                    },
                ).scalar_one()
            )
            for tag_name in _clean_tags(",".join(tags)):
                tag_id = connection.execute(
                    text(
                        """
                        INSERT INTO tag (org_id, name)
                        VALUES (:org_id, :name)
                        ON CONFLICT (org_id, name) DO UPDATE SET status = 'active'
                        RETURNING id
                        """
                    ),
                    {"org_id": org_id, "name": tag_name},
                ).scalar_one()
                connection.execute(
                    text(
                        """
                        INSERT INTO event_tag (review_id, tag_id)
                        VALUES (:review_id, :tag_id)
                        ON CONFLICT (review_id, tag_id) DO NOTHING
                        """
                    ),
                    {"review_id": review_id, "tag_id": tag_id},
                )

            current_class = connection.execute(
                text(
                    """
                    SELECT id, category_id, family_id FROM event_class
                    WHERE event_id = :event_id AND end_at IS NULL
                    FOR UPDATE
                    """
                ),
                {"event_id": event_id},
            ).mappings().one_or_none()
            old_family_id = (
                int(current_class["family_id"])
                if current_class is not None and current_class["family_id"] is not None
                else None
            )
            changed_class = current_class is None or (
                int(current_class["category_id"]) != category_id
                or old_family_id != family_id
            )
            if changed_class:
                if current_class is not None:
                    connection.execute(
                        text("UPDATE event_class SET end_at = now() WHERE id = :id"),
                        {"id": int(current_class["id"])},
                    )
                connection.execute(
                    text(
                        """
                        INSERT INTO event_class (
                            event_id, category_id, family_id, assigned_by, assignment_source
                        ) VALUES (
                            :event_id, :category_id, :family_id, :assigned_by, 'user'
                        )
                        """
                    ),
                    {
                        "event_id": event_id,
                        "category_id": category_id,
                        "family_id": family_id,
                        "assigned_by": actor_id,
                    },
                )
            changed_family = old_family_id != family_id
            connection.execute(
                text("UPDATE event SET status = 'reviewed' WHERE id = :event_id"),
                {"event_id": event_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO audit (
                        org_id, site_id, actor_id, actor_name, actor_role, action,
                        object_type, object_id, change_set, source
                    ) VALUES (
                        :org_id, :site_id, :actor_id, 'Credent manager', 'manager',
                        'review_event', 'event', :object_id,
                        CAST(:change_set AS JSONB), 'streamlit'
                    )
                    """
                ),
                {
                    "org_id": org_id,
                    "site_id": site_id,
                    "actor_id": actor_id,
                    "object_id": str(event_id),
                    "change_set": json.dumps(
                        {
                            "review_id": review_id,
                            "state": state,
                            "category_id": category_id,
                            "family_id": family_id,
                            "tags": tags,
                        },
                        sort_keys=True,
                    ),
                },
            )
    finally:
        engine.dispose()

    queued_runs: list[int] = []
    queue_errors: list[str] = []
    if changed_family:
        for affected_family_id in dict.fromkeys(
            value for value in (old_family_id, family_id) if value is not None
        ):
            if affected_family_id == old_family_id and not _family_has_members(affected_family_id):
                continue
            try:
                run_id, _ = refresh_family(
                    org_id=org_id,
                    family_id=affected_family_id,
                    actor_id=actor_id,
                )
            except Exception as error:
                queue_errors.append(str(error))
            else:
                queued_runs.append(run_id)
    return {
        "review_id": review_id,
        "family_run_ids": queued_runs,
        "family_queue_errors": queue_errors,
    }


def _family_has_members(family_id: int) -> bool:
    if active_gateway_scope() is not None:
        return bool(
            application_call("workspace", "events.family_has_members", family_id)
        )
    engine = _engine()
    if engine is None:
        return False
    try:
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM event_class
                    WHERE family_id = :family_id AND end_at IS NULL
                    """
                ),
                {"family_id": family_id},
            ).scalar_one()
            return bool(count)
    finally:
        engine.dispose()


def _event_label(event: dict[str, Any]) -> str:
    start = pd.Timestamp(event["start_date"])
    end = pd.Timestamp(event["end_date"])
    dates = f"{start:%b %-d, %Y}" if start == end else f"{start:%b %-d}–{end:%b %-d, %Y}"
    name = str(event.get("title") or "Unreviewed occurrence")
    return f"{dates} · {name}"


def _reconciliation_label(item: dict[str, Any]) -> str:
    """Short human label for one old-to-new event proposal."""

    start = item.get("source_start_date") or item.get("target_start_date")
    end = item.get("source_end_date") or item.get("target_end_date")
    if start is None:
        dates = "Unknown dates"
    else:
        start_at = pd.Timestamp(start)
        end_at = pd.Timestamp(end or start)
        dates = (
            f"{start_at:%b %-d, %Y}"
            if start_at == end_at
            else f"{start_at:%b %-d}–{end_at:%b %-d, %Y}"
        )
    title = str(item.get("source_title") or "Previously annotated event")
    relationship = str(item.get("relationship") or "same").replace("_", " ").title()
    return f"{dates} · {title} · {relationship}"


def _reconciliation_status_for_event(
    event_id: int, reconciliations: list[dict[str, Any]]
) -> dict[str, Any] | None:
    matching = [
        item for item in reconciliations
        if item.get("target_event_id") is not None
        and int(item["target_event_id"]) == event_id
    ]
    if not matching:
        return None
    priority = {"pending": 0, "auto_accepted": 1, "accepted": 2, "rejected": 3}
    return min(matching, key=lambda item: priority.get(str(item.get("decision")), 9))


def _save_reconciliation_from_ui(
    *,
    org_id: int,
    site_id: int,
    link_id: int,
    decision: str,
    actor_id: int,
    target_event_id: int | None = None,
) -> None:
    try:
        result = save_reconciliation_decision(
            org_id=org_id,
            site_id=site_id,
            link_id=link_id,
            decision=decision,
            actor_id=actor_id,
            target_event_id=target_event_id,
        )
    except Exception as error:
        st.error(f"The reconciliation decision could not be saved: {error}")
        return
    family_runs = list(result.get("family_run_ids") or [])
    if family_runs:
        st.session_state.site_family_refresh_run_id = family_runs[-1]
    if result.get("family_queue_errors"):
        st.warning(
            "The decision was saved, but affected family statistics could not be refreshed yet."
        )
    else:
        st.toast("Reconciliation decision saved.", icon="✅")
    st.session_state.event_reconciliation_drawer_open = False
    st.rerun()


@st.dialog("Event reconciliation", width="large")
def _render_reconciliation_drawer(
    *,
    org_id: int,
    site_id: int,
    actor_id: int,
    events: list[dict[str, Any]],
    reconciliations: list[dict[str, Any]],
) -> None:
    """Triage recovered annotations while leaving interpretation in the workspace."""

    counts = {
        decision: sum(1 for item in reconciliations if item.get("decision") == decision)
        for decision in DECISION_LABELS
    }
    st.caption(
        "Review how manager annotations from superseded events map to the current forecast. "
        "Interpretation, splitting, families, and tags remain in the main Events workspace."
    )
    filter_options = [
        "Needs confirmation",
        "Auto-reconciled",
        "Manager-confirmed",
        "Rejected",
        "All",
    ]
    selected_filter = st.segmented_control(
        "Reconciliation status",
        filter_options,
        default="Needs confirmation",
        key="event_reconciliation_filter",
    )
    decision_for_label = {label: decision for decision, label in DECISION_LABELS.items()}
    if selected_filter == "All":
        visible = list(reconciliations)
    else:
        decision = decision_for_label[str(selected_filter)]
        visible = [item for item in reconciliations if item.get("decision") == decision]
    st.caption(
        f"{counts['pending']} need confirmation · "
        f"{counts['auto_accepted']} auto-reconciled · "
        f"{counts['accepted']} manager-confirmed"
    )
    if not visible:
        st.info(f"No reconciliations are in “{selected_filter}”.")
        return

    item_by_id = {int(item["id"]): item for item in visible}
    selected_link_id = int(
        st.selectbox(
            "Recovered annotation",
            list(item_by_id),
            format_func=lambda value: _reconciliation_label(item_by_id[value]),
            key=f"event_reconciliation_picker_{selected_filter}",
        )
    )
    item = item_by_id[selected_link_id]
    status = DECISION_LABELS.get(str(item.get("decision")), "Unknown")
    st.markdown(f"### {status}")
    old_column, new_column = st.columns(2)
    with old_column:
        st.markdown("#### Superseded event")
        st.markdown(f"**{item.get('source_title') or 'Untitled manager annotation'}**")
        st.caption(
            f"{pd.Timestamp(item['source_start_date']):%B %-d, %Y} – "
            f"{pd.Timestamp(item['source_end_date']):%B %-d, %Y}"
        )
        if item.get("source_note"):
            st.write(str(item["source_note"]))
        details = [
            value for value in (
                item.get("source_category_name"),
                item.get("source_family_name"),
                ", ".join(map(str, item.get("source_tags") or [])) or None,
            ) if value
        ]
        if details:
            st.caption(" · ".join(map(str, details)))
    with new_column:
        st.markdown("#### Current detection")
        if item.get("target_event_id") is None:
            st.warning("No matching current detection was found.")
        else:
            st.markdown(
                f"**{pd.Timestamp(item['target_start_date']):%B %-d, %Y} – "
                f"{pd.Timestamp(item['target_end_date']):%B %-d, %Y}**"
            )
            st.caption(
                f"{str(item.get('target_direction') or '').title()} direction · "
                f"{float(item.get('target_support') or 0):.0%} model support · "
                f"{float(item.get('confidence') or 0):.0%} match confidence"
            )
    evidence = item.get("evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = {}
    if evidence:
        date_iou = float(evidence.get("date_iou") or 0)
        center_gap = float(evidence.get("center_gap_days") or 0)
        st.caption(
            f"Date overlap {date_iou:.0%} · centers {center_gap:g} day(s) apart · "
            f"relationship: {str(item.get('relationship')).replace('_', ' ')}"
        )

    target_id = item.get("target_event_id")
    if target_id is not None:
        if st.button(
            "Open current event in workspace",
            use_container_width=True,
            key=f"open_reconciled_event_{selected_link_id}",
        ):
            st.session_state.database_event_selected_id = int(target_id)
            st.session_state.event_reconciliation_drawer_open = False
            st.rerun()

    current_decision = str(item.get("decision"))
    if current_decision == "pending":
        accept, reject = st.columns(2)
        if accept.button(
            "Accept match",
            type="primary",
            use_container_width=True,
            disabled=target_id is None,
            key=f"accept_event_reconciliation_{selected_link_id}",
        ):
            _save_reconciliation_from_ui(
                org_id=org_id, site_id=site_id,
                link_id=selected_link_id, decision="accepted", actor_id=actor_id,
            )
        if reject.button(
            "Reject match",
            use_container_width=True,
            key=f"reject_event_reconciliation_{selected_link_id}",
        ):
            _save_reconciliation_from_ui(
                org_id=org_id, site_id=site_id,
                link_id=selected_link_id, decision="rejected", actor_id=actor_id,
            )
    else:
        if st.button(
            "Require manager confirmation",
            use_container_width=True,
            key=f"reopen_event_reconciliation_{selected_link_id}",
        ):
            _save_reconciliation_from_ui(
                org_id=org_id, site_id=site_id,
                link_id=selected_link_id, decision="pending", actor_id=actor_id,
            )

    event_choices = {
        int(event["id"]): event for event in events
        if target_id is None or int(event["id"]) != int(target_id)
    }
    if event_choices:
        with st.expander("Choose a different current event"):
            replacement_id = int(
                st.selectbox(
                    "Current event",
                    list(event_choices),
                    format_func=lambda value: _event_label(event_choices[value]),
                    key=f"redirect_event_reconciliation_{selected_link_id}",
                )
            )
            if st.button(
                "Use this event instead",
                use_container_width=True,
                key=f"save_event_reconciliation_redirect_{selected_link_id}",
            ):
                _save_reconciliation_from_ui(
                    org_id=org_id, site_id=site_id,
                    link_id=selected_link_id, decision="accepted",
                    actor_id=actor_id,
                    target_event_id=replacement_id,
                )


def _evidence_chart(event: dict[str, Any], observations: list[dict[str, Any]]) -> alt.Chart | None:
    start = pd.Timestamp(event["start_date"])
    end = pd.Timestamp(event["end_date"])
    frame = pd.DataFrame(observations)
    if frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["date"])
    frame["value"] = pd.to_numeric(frame["value"])
    frame = frame.loc[
        frame["date"].between(start - timedelta(days=14), end + timedelta(days=14))
    ]
    if frame.empty:
        return None
    line = alt.Chart(frame).mark_line(color="#1d2a24", strokeWidth=2.5).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("value:Q", title="Observed sales", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("value:Q", title="Sales", format=",.0f")],
    )
    window = pd.DataFrame({"start": [start], "end": [end + timedelta(days=1)]})
    band = alt.Chart(window).mark_rect(color="#d96a47", opacity=0.16).encode(
        x="start:T", x2="end:T"
    )
    return (band + line).properties(height=280)


def _render_occurrences(
    *, org_id: int, site_id: int, actor_id: int, workspace: dict[str, Any]
) -> None:
    events = list(workspace["events"])
    reconciliations = list(workspace.get("reconciliations") or [])
    if not events:
        st.info("The current forecast has no detected events to review.")
        return
    query = st.text_input(
        "Search occurrences",
        placeholder="Search names, families, categories, or tags",
        key="database_event_search",
    ).strip().lower()
    status_filter = st.selectbox(
        "Review status",
        ["All statuses", *REVIEW_STATES],
        key="database_event_status_filter",
    )
    filtered = []
    for event in events:
        label = STATE_LABELS.get(event.get("review_state"), "Needs review")
        haystack = " ".join(
            [
                str(event.get("title") or ""),
                str(event.get("family_name") or ""),
                str(event.get("category_name") or ""),
                *map(str, event.get("tags") or []),
            ]
        ).lower()
        if query and query not in haystack:
            continue
        if status_filter != "All statuses" and label != status_filter:
            continue
        filtered.append(event)
    if not filtered:
        st.info("No occurrences match those filters.")
        return
    event_by_id = {int(item["id"]): item for item in filtered}
    selected_from_drawer = st.session_state.pop("database_event_selected_id", None)
    if selected_from_drawer is not None and int(selected_from_drawer) in event_by_id:
        st.session_state.database_event_picker = int(selected_from_drawer)
    event_id = int(
        st.selectbox(
            "Occurrence",
            list(event_by_id),
            format_func=lambda value: _event_label(event_by_id[value]),
            key="database_event_picker",
        )
    )
    event = event_by_id[event_id]
    start = pd.Timestamp(event["start_date"])
    end = pd.Timestamp(event["end_date"])
    heading, badge = st.columns([3, 1])
    heading.markdown(f"### {start:%B %-d}–{end:%B %-d, %Y}")
    heading.caption(
        f"Published ERP/PIPv4 inference · {(end - start).days + 1} day(s) · "
        f"{str(event['direction']).title()} direction · "
        f"island {event['erp_island_id']}"
    )
    badge.metric("Model support", f"{float(event['support']):.0%}")
    reconciliation = _reconciliation_status_for_event(event_id, reconciliations)
    if reconciliation is not None:
        decision = str(reconciliation.get("decision"))
        if decision == "pending":
            st.warning(
                "This event has a recovered manager annotation that still needs confirmation. "
                "Open Reconciliation to compare the old and current detections."
            )
        elif decision == "auto_accepted":
            st.success(
                "This event was auto-reconciled from superseded provenance. "
                "The prior manager interpretation was carried forward and remains auditable."
            )
        elif decision == "accepted":
            st.success("A manager confirmed this recovered event match.")
        elif decision == "rejected":
            st.info("A proposed recovered-event match for this event was rejected.")
    chart = _evidence_chart(event, list(workspace["observations"]))
    if chart is not None:
        st.altair_chart(chart, use_container_width=True)
        st.caption(
            "The shaded period is the model-detected occurrence. The line is the approved sales history used by this run."
        )

    with st.container(border=True):
        st.markdown("#### Manager interpretation")
        title = st.text_input(
            "Occurrence name",
            value=str(event.get("title") or ""),
            placeholder="Example: Payday weekend",
            key=f"database_event_title_{event_id}_{event.get('review_id')}",
        )
        status_label = STATE_LABELS.get(event.get("review_state"), "Needs review")
        status = st.selectbox(
            "Review status",
            list(REVIEW_STATES),
            index=list(REVIEW_STATES).index(status_label),
            key=f"database_event_state_{event_id}_{event.get('review_id')}",
        )
        categories = {int(item["id"]): item for item in workspace["categories"]}
        category_ids = list(categories)
        current_category_id = event.get("category_id")
        category_index = (
            category_ids.index(int(current_category_id))
            if current_category_id is not None and int(current_category_id) in category_ids
            else 0
        )
        category_id = int(
            st.selectbox(
                "Category",
                category_ids,
                index=category_index,
                format_func=lambda value: str(categories[value]["name"]),
                key=f"database_event_category_{event_id}_{event.get('review_id')}",
            )
        )
        families = {
            int(item["id"]): item
            for item in workspace["families"]
            if int(item["category_id"]) == category_id
        }
        family_options: list[int | None] = [None, *families]
        current_family_id = event.get("family_id")
        family_index = (
            family_options.index(int(current_family_id))
            if current_family_id is not None and int(current_family_id) in family_options
            else 0
        )
        family_id = st.selectbox(
            "Site family",
            family_options,
            index=family_index,
            format_func=lambda value: "Unassigned" if value is None else str(families[value]["name"]),
            key=f"database_event_family_{event_id}_{event.get('review_id')}_{category_id}",
        )
        tags = st.text_input(
            "Tags",
            value=", ".join(map(str, event.get("tags") or [])),
            placeholder="recurring, weekend, promotion",
            help="Separate tags with commas.",
            key=f"database_event_tags_{event_id}_{event.get('review_id')}",
        )
        note = st.text_area(
            "Notes",
            value=str(event.get("note") or ""),
            placeholder="What happened, and why does this interpretation make sense?",
            key=f"database_event_note_{event_id}_{event.get('review_id')}",
        )
        st.info(
            "Saving creates a new manager revision. It never overwrites the original model boundary or evidence."
        )
        if st.button(
            "Save review",
            type="primary",
            use_container_width=True,
            disabled=REVIEW_STATES[status] is None or not title.strip(),
            key=f"database_event_save_{event_id}",
        ):
            try:
                result = save_event_review(
                    org_id=org_id,
                    site_id=site_id,
                    event_id=event_id,
                    title=title,
                    note=note,
                    state=str(REVIEW_STATES[status]),
                    tags=_clean_tags(tags),
                    category_id=category_id,
                    family_id=int(family_id) if family_id is not None else None,
                    actor_id=actor_id,
                )
            except Exception as error:
                st.error(f"The review could not be saved: {error}")
            else:
                runs = list(result["family_run_ids"])
                if runs:
                    st.session_state.site_family_refresh_run_id = runs[-1]
                st.toast("Event review saved.", icon="✅")
                if result["family_queue_errors"]:
                    st.warning(
                        "The review was saved, but family statistics could not be queued. "
                        "Open the Families view and refresh them manually."
                    )
                    return
                st.rerun()


def _render_classifications(workspace: dict[str, Any]) -> None:
    st.markdown("### Classification library")
    st.caption(
        "These are the live categories, site families, and tags available to manager reviews."
    )
    category_tab, family_tab, tag_tab = st.tabs(["Categories", "Site families", "Tags"])
    with category_tab:
        st.dataframe(pd.DataFrame(workspace["categories"])[["name"]], hide_index=True, use_container_width=True)
    with family_tab:
        frame = pd.DataFrame(workspace["families"])
        if frame.empty:
            st.info("No site families have been created yet.")
        else:
            st.dataframe(frame[["name", "category_name"]], hide_index=True, use_container_width=True)
    with tag_tab:
        if workspace["tags"]:
            st.dataframe(pd.DataFrame({"Tag": workspace["tags"]}), hide_index=True, use_container_width=True)
        else:
            st.info("Tags appear here after the first reviewed occurrence is saved.")


def render_database_events_workspace(
    *, org_id: int, site_id: int, site_name: str, actor_id: int
) -> None:
    """Render the durable Events workspace for one selected site."""

    try:
        workspace = load_events_workspace(site_id)
    except Exception as error:
        st.error(f"The Events workspace is temporarily unavailable: {error}")
        return
    st.markdown('<div class="dashboard-kicker">Event evidence</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="dashboard-title">Events workspace</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="dashboard-subtitle">{site_name} · Current published run '
        f'#{workspace["run_id"] or "—"}</div>',
        unsafe_allow_html=True,
    )
    if workspace["run_id"] is None:
        st.info("Publish a forecast before reviewing detected events.")
        return
    reconciliations = list(workspace.get("reconciliations") or [])
    if reconciliations:
        pending_count = sum(
            1 for item in reconciliations if item.get("decision") == "pending"
        )
        button_label = (
            f"Reconciliation · {pending_count} need confirmation"
            if pending_count
            else "Reconciliation · All reviewed"
        )
        if st.button(
            button_label,
            type="primary" if pending_count else "secondary",
            key="open_event_reconciliation_drawer",
        ):
            st.session_state.event_reconciliation_drawer_open = True
            st.session_state.event_reconciliation_filter = "Needs confirmation"
        if st.session_state.get("event_reconciliation_drawer_open", False):
            _render_reconciliation_drawer(
                org_id=org_id,
                site_id=site_id,
                actor_id=actor_id,
                events=list(workspace["events"]),
                reconciliations=reconciliations,
            )
    view = st.segmented_control(
        "Events view",
        ["Occurrences", "Families", "Classifications"],
        default="Occurrences",
        label_visibility="collapsed",
        key="database_events_view",
    )
    if view == "Families":
        render_site_family_workspace(
            org_id=org_id,
            site_id=site_id,
            actor_id=actor_id,
        )
    elif view == "Classifications":
        _render_classifications(workspace)
    else:
        _render_occurrences(
            org_id=org_id,
            site_id=site_id,
            actor_id=actor_id,
            workspace=workspace,
        )


__all__ = [
    "load_events_workspace",
    "render_database_events_workspace",
    "save_event_review",
]
