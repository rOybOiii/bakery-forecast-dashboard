"""Standalone events-library workspace for the Downtown MVP."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape

import altair as alt
import pandas as pd
import streamlit as st

from dashboard_data import disturbance_rows, history_rows
from event_review import (
    DEFAULT_EVENT_METADATA,
    FAMILY_HISTORY,
    EventSegment,
    default_annotation,
    default_segments,
    family_summary,
    validate_segments,
)


RESTAURANT = "downtown"
CATEGORIES = (
    "Unclassified",
    "Calendar",
    "Holiday",
    "Local/community",
    "Operational",
    "Promotion",
    "School/sports",
    "Weather",
)
STATUSES = ("Needs review", "Confirmed", "Unresolved", "Dismissed")
KNOWN_TAGS = (
    "annual",
    "campus",
    "downtown",
    "holiday",
    "multi-day",
    "negative-lift",
    "positive-lift",
    "recurring",
    "weekend",
    "winter",
)


def _records() -> dict[str, dict[str, object]]:
    return st.session_state.setdefault("event_review_records", {})


def _annotation(event: pd.Series) -> dict[str, object]:
    base = default_annotation(
        str(event["disturbance_id"]),
        direction=str(event["direction"]),
    )
    return {**base, **_records().get(str(event["disturbance_id"]), {})}


def _event_frame(bundle) -> pd.DataFrame:
    frame = disturbance_rows(bundle, RESTAURANT)
    return frame.loc[frame["disturbance_type"].eq("event")].copy().reset_index(drop=True)


def _signed_percent(value: float) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.1f}%"


def _evidence_chart(bundle, event: pd.Series) -> alt.Chart:
    history = history_rows(bundle, RESTAURANT).copy()
    event_start = pd.Timestamp(event["start_date"])
    event_end = pd.Timestamp(event["end_date"])
    visible = history.loc[
        history["target_date"].between(
            event_start - pd.Timedelta(days=5),
            event_end + pd.Timedelta(days=5),
        )
    ].copy()
    visible["no_event"] = visible["baseline_median"]
    active = visible["target_date"].between(event_start, event_end)
    visible["event_low"] = pd.NA
    visible["event_high"] = pd.NA
    visible.loc[active, "event_low"] = visible.loc[active, "no_event"] * (
        1 + float(event["effect_low_percent"]) / 100
    )
    visible.loc[active, "event_high"] = visible.loc[active, "no_event"] * (
        1 + float(event["effect_high_percent"]) / 100
    )

    base = alt.Chart(visible).encode(
        x=alt.X("target_date:T", title=None, axis=alt.Axis(format="%b %-d", tickCount=6))
    )
    band = base.mark_area(color="#d96a47", opacity=0.16).encode(
        y=alt.Y(
            "event_low:Q",
            title="Sales",
            axis=alt.Axis(format="$,.0f"),
            scale=alt.Scale(zero=False),
        ),
        y2="event_high:Q",
    )
    counterfactual = base.mark_line(
        color="#2f6b55", strokeDash=[6, 5], strokeWidth=2
    ).encode(
        y=alt.Y("no_event:Q", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("target_date:T", title="Date", format="%A, %b %-d"),
            alt.Tooltip("no_event:Q", title="No-event estimate", format="$,.0f"),
        ],
    )
    actual = base.mark_line(color="#d96a47", strokeWidth=3).encode(
        y=alt.Y("actual_sales:Q", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("target_date:T", title="Date", format="%A, %b %-d"),
            alt.Tooltip("actual_sales:Q", title="Actual sales", format="$,.0f"),
        ],
    )
    actual_points = base.mark_point(
        color="#d96a47", filled=True, size=48
    ).encode(y=alt.Y("actual_sales:Q", scale=alt.Scale(zero=False)))
    return (
        alt.layer(band, counterfactual, actual, actual_points)
        .properties(height=300)
        .resolve_scale(y="shared")
        .configure_view(stroke=None)
        .configure_axis(gridColor="#e6e0d5", labelColor="#6f7972", titleColor="#6f7972")
    )


def _segment_timeline(
    event_start: date,
    event_end: date,
    segments: tuple[EventSegment, ...],
) -> None:
    dates = pd.date_range(event_start, event_end, freq="D")
    cells: list[str] = []
    for current in dates:
        matching = next(
            (
                segment
                for segment in segments
                if segment.start_date <= current.date() <= segment.end_date
            ),
            None,
        )
        label = escape(matching.name) if matching else "Unassigned"
        phase_class = (
            "peak"
            if matching and "peak" in matching.name.lower()
            else "tail"
            if matching and "tail" in matching.name.lower()
            else "phase"
        )
        cells.append(
            f'<div class="event-phase-cell {phase_class}"><strong>{current:%b %-d}</strong><span>{label}</span></div>'
        )
    st.markdown(
        '<div class="event-model-boundary"><span>Original model detection</span>'
        f'<strong>{event_start:%b %-d}–{event_end:%b %-d}</strong></div>'
        f'<div class="event-phase-grid">{"".join(cells)}</div>',
        unsafe_allow_html=True,
    )


def _segments_from_editor(frame: pd.DataFrame) -> tuple[EventSegment, ...]:
    segments: list[EventSegment] = []
    for row in frame.to_dict("records"):
        if not row.get("Name") or pd.isna(row.get("Start")) or pd.isna(row.get("End")):
            continue
        segments.append(
            EventSegment(
                name=str(row["Name"]),
                start_date=pd.Timestamp(row["Start"]).date(),
                end_date=pd.Timestamp(row["End"]).date(),
                interpretation=str(row.get("Interpretation") or "Phase"),
            )
        )
    return tuple(segments)


def _render_occurrence_editor(bundle, event: pd.Series) -> None:
    event_id = str(event["disturbance_id"])
    annotation = _annotation(event)
    event_start = pd.Timestamp(event["start_date"]).date()
    event_end = pd.Timestamp(event["end_date"]).date()

    st.markdown('<div class="event-section-kicker">Selected occurrence</div>', unsafe_allow_html=True)
    title_column, evidence_column = st.columns([3, 1.4])
    with title_column:
        st.markdown(f"### {event_start:%B %-d}–{event_end:%B %-d, %Y}")
        st.caption(f"One immutable model detection · {int(event['duration_days'])} days")
    with evidence_column:
        st.markdown(
            f'<div class="event-evidence-badge">● {escape(str(event["evidence_label"]))}</div>',
            unsafe_allow_html=True,
        )

    st.altair_chart(_evidence_chart(bundle, event), width="stretch")
    st.caption(
        "Orange is actual sales, dashed green is the no-event counterfactual, and the terracotta band is the model's plausible event contribution."
    )

    st.divider()
    action_column, note_column = st.columns([1, 2])
    with action_column:
        if st.button(
            "Split into phases",
            icon=":material/call_split:",
            use_container_width=True,
            key=f"split_event_{event_id}",
        ):
            st.session_state[f"event_segments_{event_id}"] = default_segments(
                event_start, event_end
            )
            st.rerun()
    with note_column:
        st.markdown("**Manager interpretation**")
        st.caption(
            "Subdivide the detected island into phases or related events without changing the model evidence."
        )

    segments = tuple(
        st.session_state.get(
            f"event_segments_{event_id}", annotation.get("segments", ())
        )
    )
    _segment_timeline(event_start, event_end, segments)
    if segments:
        editor = pd.DataFrame(
            [
                {
                    "Name": segment.name,
                    "Start": segment.start_date,
                    "End": segment.end_date,
                    "Interpretation": segment.interpretation,
                }
                for segment in segments
            ]
        )
        edited = st.data_editor(
            editor,
            hide_index=True,
            num_rows="dynamic",
            width="stretch",
            key=f"event_segment_editor_{event_id}",
            column_config={
                "Name": st.column_config.TextColumn("Segment or event name", required=True),
                "Start": st.column_config.DateColumn(
                    "Start", min_value=event_start, max_value=event_end, required=True
                ),
                "End": st.column_config.DateColumn(
                    "End", min_value=event_start, max_value=event_end, required=True
                ),
                "Interpretation": st.column_config.SelectboxColumn(
                    "Treat as", options=["Phase", "Related event"], required=True
                ),
            },
        )
        segments = _segments_from_editor(edited)
        st.session_state[f"event_segments_{event_id}"] = segments
        errors = validate_segments(
            segments,
            event_start=event_start,
            event_end=event_end,
        )
        for error in errors:
            st.error(error)

    st.divider()
    st.markdown("#### Related history")
    family = str(annotation["family"])
    prior = FAMILY_HISTORY.get(family, ())
    if prior:
        columns = st.columns(min(3, len(prior) + 1))
        for column, occurrence in zip(columns, prior):
            with column.container(border=True):
                st.caption(str(occurrence["label"]))
                st.markdown(f"**{_signed_percent(float(occurrence['peak_percent']))} peak**")
                st.caption(f"{int(occurrence['duration_days'])} days · confirmed")
        with columns[-1].container(border=True):
            st.caption("Suggested family")
            st.markdown(f"**{family}**")
            st.caption(f"{len(prior)} prior occurrence{'s' if len(prior) != 1 else ''}")
    else:
        st.info("No prior occurrence is linked yet. Save a family to build reusable evidence.")


def _render_details(event: pd.Series) -> None:
    event_id = str(event["disturbance_id"])
    annotation = _annotation(event)
    metadata = DEFAULT_EVENT_METADATA.get(event_id, {})
    families = sorted(
        {str(item.get("family", "Unassigned")) for item in DEFAULT_EVENT_METADATA.values()}
        | {str(annotation["family"]), "Unassigned"}
    )
    with st.container(border=True):
        st.markdown('<div class="event-section-kicker">Manager interpretation</div>', unsafe_allow_html=True)
        st.markdown("### Describe this occurrence")
        name = st.text_input(
            "Occurrence name",
            value=str(annotation["name"]),
            key=f"event_name_{event_id}",
        )
        family = st.selectbox(
            "Event family",
            families,
            index=families.index(str(annotation["family"])),
            key=f"event_family_{event_id}",
        )
        if family != "Unassigned":
            st.success(
                f"Linked to {family}. Other occurrences remain separate evidence.",
                icon=":material/link:",
            )
        category = st.selectbox(
            "Category",
            CATEGORIES,
            index=CATEGORIES.index(str(annotation["category"])),
            key=f"event_category_{event_id}",
        )
        status = st.selectbox(
            "Review status",
            STATUSES,
            index=STATUSES.index(str(annotation["status"])),
            key=f"event_status_{event_id}",
        )
        tags = st.multiselect(
            "Tags",
            KNOWN_TAGS,
            default=list(annotation["tags"]),
            key=f"event_tags_{event_id}",
        )
        notes = st.text_area(
            "Notes",
            value=str(annotation["notes"]),
            placeholder="What happened, and why does this interpretation make sense?",
            key=f"event_notes_{event_id}",
        )
        st.info(
            "The original model boundary and effect remain unchanged. This saves a manager revision over that evidence.",
            icon=":material/shield:",
        )
        segments = tuple(st.session_state.get(f"event_segments_{event_id}", ()))
        errors = validate_segments(
            segments,
            event_start=pd.Timestamp(event["start_date"]).date(),
            event_end=pd.Timestamp(event["end_date"]).date(),
        )
        if st.button(
            "Save review",
            type="primary",
            use_container_width=True,
            disabled=bool(errors),
            key=f"save_event_review_{event_id}",
        ):
            _records()[event_id] = {
                "name": name.strip() or str(metadata.get("suggested_name", "Unnamed detection")),
                "suggested_name": str(metadata.get("suggested_name", "Unnamed detection")),
                "family": family,
                "category": category,
                "status": status,
                "tags": tuple(tags),
                "notes": notes,
                "segments": segments,
            }
            st.toast("Event review saved for this session.", icon="✅")


def _render_occurrences(bundle, events: pd.DataFrame) -> None:
    annotations = {str(row.disturbance_id): _annotation(row) for _, row in events.iterrows()}
    filter_columns = st.columns([2, 1, 1])
    search = filter_columns[0].text_input(
        "Search events",
        placeholder="Search names, families, or tags",
        label_visibility="collapsed",
        key="events_workspace_search",
    )
    status_filter = filter_columns[1].selectbox(
        "Status",
        ("All statuses",) + STATUSES,
        label_visibility="collapsed",
        key="events_workspace_status_filter",
    )
    category_filter = filter_columns[2].selectbox(
        "Category",
        ("All categories",) + CATEGORIES,
        label_visibility="collapsed",
        key="events_workspace_category_filter",
    )

    filtered_ids: list[str] = []
    query = search.strip().lower()
    for _, event in events.iterrows():
        event_id = str(event["disturbance_id"])
        annotation = annotations[event_id]
        haystack = " ".join(
            [
                str(annotation["name"]),
                str(annotation["family"]),
                str(annotation["category"]),
                *map(str, annotation["tags"]),
            ]
        ).lower()
        if query and query not in haystack:
            continue
        if status_filter != "All statuses" and annotation["status"] != status_filter:
            continue
        if category_filter != "All categories" and annotation["category"] != category_filter:
            continue
        filtered_ids.append(event_id)

    if not filtered_ids:
        st.info("No event occurrences match those filters.")
        return
    selected_id = st.session_state.get("events_workspace_selected")
    if selected_id not in filtered_ids:
        selected_id = filtered_ids[0]
        st.session_state.events_workspace_selected = selected_id

    library_column, evidence_column, details_column = st.columns(
        [0.9, 2.35, 1.2], gap="medium"
    )
    with library_column:
        st.markdown("#### Event occurrences")
        st.caption(f"{len(filtered_ids)} detections in this view")
        for event_id in filtered_ids:
            event = events.loc[events["disturbance_id"].eq(event_id)].iloc[0]
            annotation = annotations[event_id]
            start = pd.Timestamp(event["start_date"])
            end = pd.Timestamp(event["end_date"])
            label = (
                f"**{annotation['name']}**  \n"
                f"{start:%b %-d}–{end:%b %-d, %Y} · "
                f"{_signed_percent(float(event['effect_median_percent']))} daily"
            )
            if st.button(
                label,
                type="primary" if event_id == selected_id else "secondary",
                use_container_width=True,
                key=f"event_candidate_{event_id}",
            ):
                st.session_state.events_workspace_selected = event_id
                st.rerun()
            st.caption(
                f"{annotation['category']} · {annotation['status']} · {event['evidence_label']}"
            )
    selected = events.loc[events["disturbance_id"].eq(selected_id)].iloc[0]
    with evidence_column:
        with st.container(border=True):
            _render_occurrence_editor(bundle, selected)
    with details_column:
        _render_details(selected)


def _render_families(events: pd.DataFrame) -> None:
    grouped: dict[str, list[dict[str, object]]] = {}
    for _, event in events.iterrows():
        annotation = _annotation(event)
        family = str(annotation["family"])
        if family == "Unassigned":
            continue
        grouped.setdefault(family, []).append(
            {
                "label": str(annotation["name"]),
                "date": pd.Timestamp(event["start_date"]),
                "peak_percent": float(event["effect_median_percent"]),
                "duration_days": int(event["duration_days"]),
                "status": str(annotation["status"]),
            }
        )
    if not grouped:
        st.info("Name and link an occurrence to create the first event family.")
        return
    family_names = sorted(grouped)
    selected_family = st.session_state.get("events_workspace_family", family_names[0])
    if selected_family not in family_names:
        selected_family = family_names[0]
    family_column, detail_column = st.columns([1, 2.4], gap="large")
    with family_column:
        st.markdown("#### Known families")
        for family in family_names:
            summary = family_summary(family, grouped[family])
            if st.button(
                f"**{family}**  \n{summary['occurrence_count']} occurrences · "
                f"{_signed_percent(float(summary['typical_peak_percent']))} typical peak",
                type="primary" if family == selected_family else "secondary",
                use_container_width=True,
                key=f"event_family_card_{family}",
            ):
                st.session_state.events_workspace_family = family
                st.rerun()
    summary = family_summary(selected_family, grouped[selected_family])
    with detail_column:
        with st.container(border=True):
            st.markdown('<div class="event-section-kicker">Selected family</div>', unsafe_allow_html=True)
            st.markdown(f"## {selected_family}")
            st.caption("Repeated occurrences remain separate evidence and contribute to this family summary.")
            metric_columns = st.columns(3)
            metric_columns[0].metric("Occurrences", int(summary["occurrence_count"]))
            metric_columns[1].metric(
                "Typical peak", _signed_percent(float(summary["typical_peak_percent"]))
            )
            metric_columns[2].metric(
                "Typical duration", f"{float(summary['typical_duration_days']):.0f} days"
            )
            st.markdown("#### Linked occurrence evidence")
            rows = grouped[selected_family] + list(FAMILY_HISTORY.get(selected_family, ()))
            table = pd.DataFrame(rows)
            if "date" in table:
                table["date"] = pd.to_datetime(table["date"], errors="coerce").dt.strftime("%b %-d, %Y")
            table = table.rename(
                columns={
                    "label": "Occurrence",
                    "date": "Date",
                    "peak_percent": "Peak effect",
                    "duration_days": "Duration",
                    "status": "Status",
                }
            )
            if "Peak effect" in table:
                table["Peak effect"] = table["Peak effect"].map(_signed_percent)
            if "Duration" in table:
                table["Duration"] = table["Duration"].map(lambda value: f"{int(value)} days")
            st.dataframe(table, hide_index=True, width="stretch")


def render_events_workspace(bundle) -> None:
    st.markdown(
        """
        <style>
        .events-workspace-title {
            color: var(--ink); font-size: clamp(2rem, 3.4vw, 3rem); font-weight: 750;
            letter-spacing: -0.04em; line-height: 1.04; margin: 0 0 .2rem;
        }
        .event-section-kicker {
            color: var(--green); font-size: .7rem; font-weight: 750;
            letter-spacing: .11em; text-transform: uppercase; margin-bottom: .25rem;
        }
        .event-evidence-badge {
            background: #dfe9e1; color: #2f6b55; border-radius: 999px;
            padding: .45rem .65rem; text-align: center; white-space: nowrap;
            font-size: .76rem; font-weight: 650; margin-top: .35rem;
        }
        [class*="st-key-event_candidate_"] button,
        [class*="st-key-event_family_card_"] button {
            min-height: 4rem; justify-content: flex-start; text-align: left;
            white-space: normal; line-height: 1.25; padding: .7rem .8rem;
        }
        .event-model-boundary {
            display: flex; justify-content: space-between; gap: .5rem;
            background: #eee9dd; color: var(--muted); border-radius: 8px;
            padding: .55rem .7rem; margin: .75rem 0 .4rem; font-size: .74rem;
        }
        .event-model-boundary strong { color: var(--ink); }
        .event-phase-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(70px, 1fr));
            gap: .22rem; margin-bottom: .75rem;
        }
        .event-phase-cell {
            min-height: 3.1rem; background: #dfe9e1; color: #2f6b55;
            border-radius: 7px; padding: .45rem; display: flex;
            flex-direction: column; justify-content: center; text-align: center;
        }
        .event-phase-cell.peak { background: #f4d9ce; color: #a84e34; }
        .event-phase-cell.tail { background: #e7e1ee; color: #685b88; }
        .event-phase-cell strong { font-size: .72rem; }
        .event-phase-cell span { font-size: .66rem; margin-top: .12rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="dashboard-kicker">Event evidence</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="events-workspace-title">Events workspace</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Turn model detections into named, classified, reusable event history.</div>',
        unsafe_allow_html=True,
    )
    events = _event_frame(bundle)
    view = st.segmented_control(
        "Events view",
        ["Occurrences", "Families"],
        default="Occurrences",
        label_visibility="collapsed",
        key="events_workspace_view",
    )
    if view == "Families":
        _render_families(events)
    else:
        _render_occurrences(bundle, events)
    st.caption(
        "MVP annotations are retained only for this Streamlit session. Model detections are read-only."
    )
