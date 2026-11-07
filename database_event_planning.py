"""Database-backed future-event imputation for the Forecast page."""

from __future__ import annotations

from datetime import date, timedelta
import json
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text
from gateway_client import active_gateway_scope, application_call

from database_event_families import _engine


def load_event_imputation_workspace(
    *, site_id: int, metric_id: int, forecast_id: int
) -> dict[str, Any]:
    if active_gateway_scope() is not None:
        return application_call(
            "workspace",
            "events.load_imputation",
            site_id=site_id,
            metric_id=metric_id,
            forecast_id=forecast_id,
        )
    """Load reviewed family estimates and the current plan for one vintage."""

    engine = _engine()
    if engine is None:
        return {"families": [], "blocked_families": [], "plans": [], "horizon": []}
    try:
        with engine.connect() as connection:
            horizon = [
                row.isoformat()
                for row in connection.execute(
                    text(
                        """
                        SELECT date FROM prediction
                        WHERE forecast_id = :forecast_id
                        ORDER BY date
                        """
                    ),
                    {"forecast_id": forecast_id},
                ).scalars()
            ]
            families = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT family.id, family.name, family.category_id,
                            category.name AS category_name,
                            estimate.id AS estimate_id, estimate.unit,
                            estimate.quality, estimate.direction,
                            estimate.stability, estimate.statistics,
                            estimate.version AS estimate_version,
                            estimate.member_count
                        FROM family
                        JOIN category ON category.id = family.category_id
                        JOIN current_family_estimate estimate
                          ON estimate.family_id = family.id
                         AND estimate.site_id = family.site_id
                        JOIN model_run estimate_run
                          ON estimate_run.id = estimate.run_id
                        JOIN model_profile estimate_profile
                          ON estimate_profile.id = estimate_run.profile_id
                         AND estimate_profile.metric_id = :metric_id
                        WHERE family.site_id = :site_id
                          AND family.status = 'active'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM event_reconciliation_link unresolved
                              LEFT JOIN event_class source_class
                                ON source_class.event_id = unresolved.source_event_id
                               AND source_class.end_at IS NULL
                              LEFT JOIN event_class target_class
                                ON target_class.event_id = unresolved.target_event_id
                               AND target_class.end_at IS NULL
                              WHERE unresolved.decision = 'pending'
                                AND (
                                    source_class.family_id = family.id
                                    OR target_class.family_id = family.id
                                )
                          )
                        ORDER BY family.name
                        """
                    ),
                    {"site_id": site_id, "metric_id": metric_id},
                ).mappings()
            ]
            for family in families:
                family["shape"] = [
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
                        {"estimate_id": int(family["estimate_id"])},
                    ).mappings()
                ]

            blocked_families = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT family.id, family.name,
                            COUNT(DISTINCT unresolved.id) AS pending_count
                        FROM family
                        JOIN event_class member_class
                          ON member_class.family_id = family.id
                         AND member_class.end_at IS NULL
                        JOIN event_reconciliation_link unresolved
                          ON unresolved.decision = 'pending'
                         AND (
                            unresolved.source_event_id = member_class.event_id
                            OR unresolved.target_event_id = member_class.event_id
                         )
                        WHERE family.site_id = :site_id
                          AND family.status = 'active'
                        GROUP BY family.id, family.name
                        ORDER BY family.name
                        """
                    ),
                    {"site_id": site_id},
                ).mappings()
            ]

            plans = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id, parent_id, title, status, created_at
                        FROM event_plan
                        WHERE forecast_id = :forecast_id
                          AND status IN ('draft', 'implemented')
                        ORDER BY CASE status WHEN 'draft' THEN 0 ELSE 1 END,
                            created_at DESC, id DESC
                        """
                    ),
                    {"forecast_id": forecast_id},
                ).mappings()
            ]
            for plan in plans:
                plan["placements"] = _load_plan_placements(
                    connection, int(plan["id"])
                )
    finally:
        engine.dispose()
    return {
        "families": families,
        "blocked_families": blocked_families,
        "plans": plans,
        "horizon": horizon,
    }


def _load_plan_placements(connection, plan_id: int) -> list[dict[str, Any]]:
    placements = [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT planned.id, planned.title, planned.category_id,
                    planned.family_id, planned.family_estimate_id,
                    planned.start_date, planned.end_date, planned.scale,
                    planned.repeat, family.name AS family_name,
                    category.name AS category_name
                FROM planned_event planned
                JOIN category ON category.id = planned.category_id
                LEFT JOIN family ON family.id = planned.family_id
                WHERE planned.plan_id = :plan_id
                  AND planned.status = 'active'
                ORDER BY planned.start_date, planned.id
                """
            ),
            {"plan_id": plan_id},
        ).mappings()
    ]
    for placement in placements:
        placement["effects"] = [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT day, low, median, high
                    FROM event_effect
                    WHERE planned_id = :planned_id
                    ORDER BY day
                    """
                ),
                {"planned_id": int(placement["id"])},
            ).mappings()
        ]
    return placements


def save_event_imputation_plan(
    *,
    org_id: int,
    site_id: int,
    metric_id: int,
    forecast_id: int,
    title: str,
    placements: list[dict[str, Any]],
    implement: bool,
    actor_id: int,
) -> int:
    """Create one immutable draft or implemented event-plan revision."""

    if active_gateway_scope() is not None:
        return int(
            application_call(
                "workspace",
                "events.save_imputation",
                org_id=org_id,
                site_id=site_id,
                metric_id=metric_id,
                forecast_id=forecast_id,
                title=title,
                placements=placements,
                implement=implement,
                actor_id=actor_id,
            )
        )
    clean_title = " ".join(title.split()) or "Forecast event plan"
    engine = _engine()
    if engine is None:
        raise RuntimeError("The local database service is unavailable.")
    try:
        with engine.begin() as connection:
            forecast = connection.execute(
                text(
                    """
                    SELECT id, site_id, metric_id
                    FROM current_forecast
                    WHERE id = :forecast_id AND site_id = :site_id
                      AND metric_id = :metric_id
                    """
                ),
                {
                    "forecast_id": forecast_id,
                    "site_id": site_id,
                    "metric_id": metric_id,
                },
            ).mappings().one_or_none()
            if forecast is None:
                raise ValueError(
                    "That forecast is no longer current. Reopen the drawer on the latest forecast."
                )
            horizon = set(
                connection.execute(
                    text(
                        "SELECT date FROM prediction WHERE forecast_id = :forecast_id"
                    ),
                    {"forecast_id": forecast_id},
                ).scalars()
            )
            parent_id = connection.execute(
                text(
                    """
                    SELECT id FROM event_plan
                    WHERE site_id = :site_id AND forecast_id = :forecast_id
                      AND status IN ('draft', 'implemented')
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    FOR UPDATE
                    """
                ),
                {"site_id": site_id, "forecast_id": forecast_id},
            ).scalar_one_or_none()
            normalized: list[dict[str, Any]] = []
            for raw in placements:
                family_id = int(raw["family_id"])
                estimate_id = int(raw["family_estimate_id"])
                family = connection.execute(
                    text(
                        """
                        SELECT family.id, family.category_id, family.name,
                            estimate.id AS estimate_id
                        FROM family
                        JOIN current_family_estimate estimate
                          ON estimate.family_id = family.id
                         AND estimate.site_id = family.site_id
                        JOIN model_run estimate_run
                          ON estimate_run.id = estimate.run_id
                        JOIN model_profile estimate_profile
                          ON estimate_profile.id = estimate_run.profile_id
                         AND estimate_profile.metric_id = :metric_id
                        WHERE family.id = :family_id
                          AND family.site_id = :site_id
                          AND family.status = 'active'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM event_reconciliation_link unresolved
                              LEFT JOIN event_class source_class
                                ON source_class.event_id = unresolved.source_event_id
                               AND source_class.end_at IS NULL
                              LEFT JOIN event_class target_class
                                ON target_class.event_id = unresolved.target_event_id
                               AND target_class.end_at IS NULL
                              WHERE unresolved.decision = 'pending'
                                AND (
                                    source_class.family_id = family.id
                                    OR target_class.family_id = family.id
                                )
                          )
                        """
                    ),
                    {
                        "family_id": family_id,
                        "site_id": site_id,
                        "metric_id": metric_id,
                    },
                ).mappings().one_or_none()
                if family is None or int(family["estimate_id"]) != estimate_id:
                    raise ValueError(
                        "One selected event family is awaiting reconciliation or has newer statistics. "
                        "Reopen the drawer after reconciling it."
                    )
                start_date = pd.Timestamp(raw["start_date"]).date()
                effects = list(raw.get("effects") or [])
                if not effects:
                    raise ValueError("Every planned event needs at least one effect day.")
                covered_dates = [
                    start_date + timedelta(days=offset)
                    for offset in range(len(effects))
                ]
                if any(value not in horizon for value in covered_dates):
                    raise ValueError(
                        "Every planned event day must fall inside the current forecast."
                    )
                normalized_effects = []
                for expected_day, effect in enumerate(effects):
                    low = float(effect["low"])
                    median = float(effect["median"])
                    high = float(effect["high"])
                    if not low <= median <= high:
                        raise ValueError(
                            "Event uncertainty must satisfy low ≤ estimate ≤ high."
                        )
                    normalized_effects.append(
                        {"day": expected_day, "low": low, "median": median, "high": high}
                    )
                normalized.append(
                    {
                        "title": " ".join(str(raw.get("title") or family["name"]).split()),
                        "family_id": family_id,
                        "family_estimate_id": estimate_id,
                        "category_id": int(family["category_id"]),
                        "start_date": start_date,
                        "end_date": covered_dates[-1],
                        "scale": float(raw.get("scale") or 1.0),
                        "repeat": dict(raw.get("repeat") or {}),
                        "effects": normalized_effects,
                    }
                )

            if implement:
                connection.execute(
                    text(
                        """
                        UPDATE event_plan
                        SET status = CASE
                            WHEN status = 'implemented' THEN 'superseded'
                            ELSE 'discarded'
                        END
                        WHERE site_id = :site_id AND forecast_id = :forecast_id
                          AND status IN ('draft', 'implemented')
                        """
                    ),
                    {"site_id": site_id, "forecast_id": forecast_id},
                )
            else:
                connection.execute(
                    text(
                        """
                        UPDATE event_plan SET status = 'discarded'
                        WHERE site_id = :site_id AND forecast_id = :forecast_id
                          AND status = 'draft'
                        """
                    ),
                    {"site_id": site_id, "forecast_id": forecast_id},
                )
            plan_id = int(
                connection.execute(
                    text(
                        """
                        INSERT INTO event_plan (
                            site_id, forecast_id, parent_id, created_by,
                            title, status
                        ) VALUES (
                            :site_id, :forecast_id, :parent_id, :created_by,
                            :title, :status
                        ) RETURNING id
                        """
                    ),
                    {
                        "site_id": site_id,
                        "forecast_id": forecast_id,
                        "parent_id": parent_id,
                        "created_by": actor_id,
                        "title": clean_title,
                        "status": "implemented" if implement else "draft",
                    },
                ).scalar_one()
            )
            for placement in normalized:
                planned_id = int(
                    connection.execute(
                        text(
                            """
                            INSERT INTO planned_event (
                                plan_id, title, category_id, family_id,
                                family_estimate_id, source, start_date,
                                end_date, scale, repeat
                            ) VALUES (
                                :plan_id, :title, :category_id, :family_id,
                                :family_estimate_id, 'family', :start_date,
                                :end_date, :scale, CAST(:repeat AS JSONB)
                            ) RETURNING id
                            """
                        ),
                        {
                            "plan_id": plan_id,
                            "title": placement["title"],
                            "category_id": placement["category_id"],
                            "family_id": placement["family_id"],
                            "family_estimate_id": placement["family_estimate_id"],
                            "start_date": placement["start_date"],
                            "end_date": placement["end_date"],
                            "scale": placement["scale"],
                            "repeat": json.dumps(placement["repeat"], sort_keys=True),
                        },
                    ).scalar_one()
                )
                for effect in placement["effects"]:
                    connection.execute(
                        text(
                            """
                            INSERT INTO event_effect (
                                planned_id, day, low, median, high
                            ) VALUES (
                                :planned_id, :day, :low, :median, :high
                            )
                            """
                        ),
                        {"planned_id": planned_id, **effect},
                    )
            connection.execute(
                text(
                    """
                    INSERT INTO audit (
                        org_id, site_id, actor_id, actor_name, actor_role,
                        action, object_type, object_id, change_set, source
                    ) VALUES (
                        :org_id, :site_id, :actor_id,
                        'Credent manager', 'manager', :action,
                        'event_plan', :object_id,
                        CAST(:change_set AS JSONB), 'streamlit'
                    )
                    """
                ),
                {
                    "org_id": org_id,
                    "site_id": site_id,
                    "actor_id": actor_id,
                    "action": "implement_event_plan" if implement else "save_event_plan_draft",
                    "object_id": str(plan_id),
                    "change_set": json.dumps(
                        {
                            "forecast_id": forecast_id,
                            "parent_id": parent_id,
                            "placement_count": len(normalized),
                        },
                        sort_keys=True,
                    ),
                },
            )
            return plan_id
    finally:
        engine.dispose()


def load_implemented_event_effects(forecast_id: int) -> dict[str, Any] | None:
    if active_gateway_scope() is not None:
        return application_call("workspace", "events.load_implemented", forecast_id)
    """Load the current implemented overlay for one exact forecast vintage."""

    engine = _engine()
    if engine is None:
        return None
    try:
        with engine.connect() as connection:
            plan = connection.execute(
                text(
                    """
                    SELECT id, title, created_at
                    FROM event_plan
                    WHERE forecast_id = :forecast_id AND status = 'implemented'
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """
                ),
                {"forecast_id": forecast_id},
            ).mappings().one_or_none()
            if plan is None:
                return None
            points = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT planned.start_date + effect.day AS date,
                            SUM(effect.low) AS low,
                            SUM(effect.median) AS median,
                            SUM(effect.high) AS high
                        FROM planned_event planned
                        JOIN event_effect effect ON effect.planned_id = planned.id
                        WHERE planned.plan_id = :plan_id
                          AND planned.status = 'active'
                        GROUP BY planned.start_date + effect.day
                        ORDER BY date
                        """
                    ),
                    {"plan_id": int(plan["id"])},
                ).mappings()
            ]
            placements = _load_plan_placements(connection, int(plan["id"]))
    finally:
        engine.dispose()
    return {"plan": dict(plan), "points": points, "placements": placements}


def _shape_chart(shape: list[dict[str, Any]]) -> alt.Chart:
    frame = pd.DataFrame(shape)
    for column in ("low", "median", "high"):
        frame[column] = pd.to_numeric(frame[column])
    frame["day"] = frame["day"].astype(int) + 1
    band = alt.Chart(frame).mark_area(color="#d96a47", opacity=0.18).encode(
        x=alt.X("day:O", title="Event day"),
        y=alt.Y("low:Q", title="Expected sales effect"),
        y2="high:Q",
    )
    line = alt.Chart(frame).mark_line(color="#b65438", strokeWidth=3).encode(
        x="day:O", y="median:Q", tooltip=["day:O", "low:Q", "median:Q", "high:Q"]
    )
    return (band + line).properties(height=190)


def _working_key(forecast_id: int) -> str:
    return f"event_imputation_working_{forecast_id}"


def _initial_working(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    plan = next(
        (item for item in workspace["plans"] if item["status"] == "draft"),
        None,
    ) or next(
        (item for item in workspace["plans"] if item["status"] == "implemented"),
        None,
    )
    if plan is None:
        return []
    return [
        {
            "title": str(item.get("title") or item.get("family_name") or "Planned event"),
            "family_id": int(item["family_id"]),
            "family_estimate_id": int(item["family_estimate_id"]),
            "start_date": pd.Timestamp(item["start_date"]).date(),
            "scale": float(item["scale"]),
            "repeat": dict(item.get("repeat") or {}),
            "effects": [
                {
                    "day": int(effect["day"]),
                    "low": float(effect["low"]),
                    "median": float(effect["median"]),
                    "high": float(effect["high"]),
                }
                for effect in item["effects"]
            ],
        }
        for item in plan["placements"]
    ]


@st.dialog(
    "Impute forecast events",
    width="small",
    dismissible=False,
    position="right",
)
def render_event_imputation_drawer(
    *, org_id: int, site_id: int, metric_id: int, forecast_id: int, actor_id: int
) -> None:
    """Render future-event planning without mixing in event interpretation."""

    try:
        workspace = load_event_imputation_workspace(
            site_id=site_id,
            metric_id=metric_id,
            forecast_id=forecast_id,
        )
    except Exception as error:
        st.error(f"Event planning is temporarily unavailable: {error}")
        return
    st.caption(
        "Place a reviewed event family into this forecast. Interpretation, splitting, "
        "family membership, and tags remain in the Events workspace."
    )
    blocked_families = list(workspace.get("blocked_families") or [])
    if blocked_families:
        names = ", ".join(str(item["name"]) for item in blocked_families[:3])
        extra = len(blocked_families) - 3
        if extra > 0:
            names += f" and {extra} more"
        st.warning(
            f"Waiting for event reconciliation: {names}. "
            "Auto-reconcile or manager-confirm those events before using their families here."
        )
    if not workspace["horizon"]:
        st.info("This forecast has no future dates available for event planning.")
        return
    if not workspace["families"]:
        message = (
            "No event family is currently eligible for imputation. Complete the pending "
            "event reconciliation first."
            if blocked_families
            else "No reviewed event family has a current estimate yet. Use the Events "
            "workspace to interpret occurrences and build a family first."
        )
        st.info(message)
        if st.button("Close", use_container_width=True):
            st.session_state.event_imputation_drawer_open = False
            st.rerun()
        return

    key = _working_key(forecast_id)
    if key not in st.session_state:
        st.session_state[key] = _initial_working(workspace)
    working = list(st.session_state[key])
    family_by_id = {int(item["id"]): item for item in workspace["families"]}
    query = st.text_input(
        "Search reviewed families",
        placeholder="Search family or category",
        key=f"event_imputation_search_{forecast_id}",
    ).strip().lower()
    options = [
        family_id
        for family_id, item in family_by_id.items()
        if not query
        or query in f"{item['name']} {item['category_name']}".lower()
    ]
    if not options:
        st.info("No reviewed event family matches that search.")
        return
    family_id = int(
        st.selectbox(
            "Event family",
            options,
            format_func=lambda value: (
                f"{family_by_id[value]['name']} · "
                f"{family_by_id[value]['category_name']}"
            ),
            key=f"event_imputation_family_{forecast_id}",
        )
    )
    family = family_by_id[family_id]
    st.altair_chart(_shape_chart(list(family["shape"])), use_container_width=True)
    st.caption(
        f"{int(family['member_count'])} reviewed occurrence(s) · "
        f"{str(family['quality']).title()} evidence · estimate version "
        f"{int(family['estimate_version'])}"
    )

    horizon_start = pd.Timestamp(workspace["horizon"][0]).date()
    horizon_end = pd.Timestamp(workspace["horizon"][-1]).date()
    shape = list(family["shape"])
    start = st.date_input(
        "Start date",
        value=horizon_start,
        min_value=horizon_start,
        max_value=horizon_end,
        key=f"event_imputation_start_{forecast_id}_{family_id}",
    )
    maximum_days = (horizon_end - start).days + 1
    usable_shape = shape[:maximum_days]
    if len(usable_shape) < len(shape):
        st.caption("The family shape is clipped at the end of this forecast window.")
    occurrence = st.segmented_control(
        "Occurrence",
        ["One time", "Recurring"],
        default="One time",
        key=f"event_imputation_occurrence_{forecast_id}_{family_id}",
        width="stretch",
    ) or "One time"
    every_weeks = 1
    if occurrence == "Recurring":
        every_weeks = int(
            st.number_input(
                "Repeat every (weeks)",
                min_value=1,
                max_value=4,
                value=1,
                step=1,
                key=f"event_imputation_repeat_{forecast_id}_{family_id}",
            )
        )
    magnitude = int(
        st.slider(
            "Overall magnitude",
            min_value=50,
            max_value=160,
            value=100,
            step=5,
            format="%d%%",
            key=f"event_imputation_magnitude_{forecast_id}_{family_id}",
        )
    )
    scaled = [
        {
            "day": index,
            "low": float(item["low"]) * magnitude / 100.0,
            "median": float(item["median"]) * magnitude / 100.0,
            "high": float(item["high"]) * magnitude / 100.0,
        }
        for index, item in enumerate(usable_shape)
    ]
    if st.toggle(
        "Adjust individual days",
        key=f"event_imputation_adjust_{forecast_id}_{family_id}",
    ):
        for item in scaled:
            day_number = int(item["day"]) + 1
            median = float(
                st.number_input(
                    f"Day {day_number} expected effect",
                    value=float(item["median"]),
                    step=10.0,
                    key=f"event_imputation_day_{forecast_id}_{family_id}_{day_number}",
                )
            )
            original = float(item["median"])
            ratio = median / original if original else 1.0
            low, high = sorted(
                (float(item["low"]) * ratio, float(item["high"]) * ratio)
            )
            item.update(low=min(low, median), median=median, high=max(high, median))

    if st.button("Add to forecast timeline", type="primary", use_container_width=True):
        starts = [start]
        if occurrence == "Recurring":
            cursor = start + timedelta(weeks=every_weeks)
            while cursor <= horizon_end:
                starts.append(cursor)
                cursor += timedelta(weeks=every_weeks)
        for event_start in starts:
            available = (horizon_end - event_start).days + 1
            effects = [dict(item) for item in scaled[:available]]
            working.append(
                {
                    "title": str(family["name"]),
                    "family_id": family_id,
                    "family_estimate_id": int(family["estimate_id"]),
                    "start_date": event_start,
                    "scale": magnitude / 100.0,
                    "repeat": {
                        "occurrence": occurrence,
                        "every_weeks": every_weeks if occurrence == "Recurring" else None,
                        "customized": bool(
                            st.session_state.get(
                                f"event_imputation_adjust_{forecast_id}_{family_id}"
                            )
                        ),
                    },
                    "effects": effects,
                }
            )
        st.session_state[key] = working
        st.rerun()

    st.divider()
    st.markdown(f"**Event plan · {len(working)} placement(s)**")
    if not working:
        st.caption("No known future events are placed in this forecast.")
    for index, placement in enumerate(working):
        event_start = pd.Timestamp(placement["start_date"]).date()
        event_end = event_start + timedelta(days=len(placement["effects"]) - 1)
        label = (
            f"{placement['title']} · {event_start:%b %-d}–{event_end:%b %-d}"
        )
        row, remove = st.columns([5, 1])
        row.caption(label)
        if remove.button(
            "×", key=f"remove_event_imputation_{forecast_id}_{index}", help="Remove"
        ):
            working.pop(index)
            st.session_state[key] = working
            st.rerun()

    title = st.text_input(
        "Plan name",
        value="Forecast event plan",
        key=f"event_imputation_title_{forecast_id}",
    )
    save, implement = st.columns(2)
    if save.button("Save draft", use_container_width=True):
        try:
            plan_id = save_event_imputation_plan(
                org_id=org_id,
                site_id=site_id,
                metric_id=metric_id,
                forecast_id=forecast_id,
                title=title,
                placements=working,
                implement=False,
                actor_id=actor_id,
            )
        except Exception as error:
            st.error(f"The event draft could not be saved: {error}")
        else:
            st.toast(f"Event draft #{plan_id} saved.", icon="✅")
            st.session_state.event_imputation_drawer_open = False
            st.session_state.pop(key, None)
            st.rerun()
    if implement.button(
        "Implement", type="primary", use_container_width=True
    ):
        try:
            plan_id = save_event_imputation_plan(
                org_id=org_id,
                site_id=site_id,
                metric_id=metric_id,
                forecast_id=forecast_id,
                title=title,
                placements=working,
                implement=True,
                actor_id=actor_id,
            )
        except Exception as error:
            st.error(f"The event plan could not be implemented: {error}")
        else:
            st.toast(f"Event plan #{plan_id} implemented.", icon="✅")
            st.session_state.event_imputation_drawer_open = False
            st.session_state.pop(key, None)
            st.rerun()
    if st.button("Close", use_container_width=True):
        st.session_state.event_imputation_drawer_open = False
        st.rerun()


__all__ = [
    "load_event_imputation_workspace",
    "load_implemented_event_effects",
    "render_event_imputation_drawer",
    "save_event_imputation_plan",
]
