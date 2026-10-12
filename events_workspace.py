"""Standalone events-library workspace for the Downtown MVP."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape
import re

import altair as alt
import pandas as pd
import streamlit as st

from dashboard_data import disturbance_rows, history_rows
from event_review import (
    DEMO_SPLIT_EVIDENCE,
    DEFAULT_EVENT_METADATA,
    EVENT_PHASES,
    FAMILY_HISTORY,
    EventSegment,
    auto_phase_assignments,
    default_annotation,
    events_in_window,
    family_summary,
    segments_from_phase_assignments,
    validate_phase_assignments,
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


def _taxonomy() -> dict[str, dict[str, dict[str, object]]]:
    families = {
        str(item.get("family")): {"active": True, "description": ""}
        for item in DEFAULT_EVENT_METADATA.values()
        if item.get("family")
    }
    categories = {
        name: {"active": True, "description": ""}
        for name in CATEGORIES
        if name != "Unclassified"
    }
    tags = {name: {"active": True, "description": ""} for name in KNOWN_TAGS}
    return st.session_state.setdefault(
        "event_taxonomy",
        {"families": families, "categories": categories, "tags": tags},
    )


def _catalog_names(kind: str) -> list[str]:
    return sorted(
        name
        for name, values in _taxonomy()[kind].items()
        if bool(values.get("active", True))
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _create_catalog_item(kind: str, name: str, description: str = "") -> bool:
    clean = " ".join(name.split())
    if not clean:
        return False
    catalog = _taxonomy()[kind]
    existing = next((item for item in catalog if item.casefold() == clean.casefold()), None)
    if existing:
        catalog[existing]["active"] = True
        return False
    catalog[clean] = {"active": True, "description": description.strip()}
    return True


def _records() -> dict[str, dict[str, object]]:
    return st.session_state.setdefault("event_review_records", {})


def _split_decisions() -> dict[str, dict[str, object]]:
    return st.session_state.setdefault("event_split_decisions", {})


def _annotation(event: pd.Series) -> dict[str, object]:
    base = default_annotation(
        str(event["disturbance_id"]),
        direction=str(event["direction"]),
    )
    return {**base, **_records().get(str(event["disturbance_id"]), {})}


def _event_frame(bundle) -> pd.DataFrame:
    frame = disturbance_rows(bundle, RESTAURANT)
    frame = frame.loc[frame["disturbance_type"].eq("event")].copy().reset_index(drop=True)
    expanded: list[pd.Series] = []
    for _, event in frame.iterrows():
        event_id = str(event["disturbance_id"])
        decision = _split_decisions().get(event_id)
        if not decision:
            event["split_parent_id"] = ""
            event["erp_peak_date"] = pd.NaT
            expanded.append(event)
            continue
        cut = pd.Timestamp(decision["cut_date"])
        for suffix, child_start, child_end, peak in (
            (
                "a",
                pd.Timestamp(event["start_date"]),
                cut,
                pd.Timestamp(decision["left_peak_date"]),
            ),
            (
                "b",
                cut + pd.Timedelta(days=1),
                pd.Timestamp(event["end_date"]),
                pd.Timestamp(decision["right_peak_date"]),
            ),
        ):
            child = event.copy()
            child["disturbance_id"] = f"{event_id}::{suffix}"
            child["split_parent_id"] = event_id
            child["erp_peak_date"] = peak
            child["start_date"] = child_start
            child["end_date"] = child_end
            child["duration_days"] = (child_end - child_start).days + 1
            expanded.append(child)
    return pd.DataFrame(expanded).reset_index(drop=True)


def _signed_percent(value: float) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.1f}%"


def _family_history(family: str) -> tuple[dict[str, object], ...]:
    aliases = st.session_state.setdefault("event_family_history_aliases", {})
    source = aliases.get(family, family)
    return FAMILY_HISTORY.get(source, ())


def _rename_catalog_item(kind: str, old_name: str, new_name: str, description: str) -> bool:
    clean = " ".join(new_name.split())
    catalog = _taxonomy()[kind]
    if not clean or (clean != old_name and clean in catalog):
        return False
    values = dict(catalog.pop(old_name))
    values["description"] = description.strip()
    catalog[clean] = values
    field = {"families": "family", "categories": "category"}.get(kind)
    for record in _records().values():
        if field and record.get(field) == old_name:
            record[field] = clean
        elif kind == "tags":
            record["tags"] = tuple(
                clean if tag == old_name else tag for tag in record.get("tags", ())
            )
    if kind == "families" and clean != old_name:
        aliases = st.session_state.setdefault("event_family_history_aliases", {})
        aliases[clean] = aliases.pop(old_name, old_name)
    return True


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
            else "crest"
            if matching and "crest" in matching.name.lower()
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


def _phase_values() -> dict[str, tuple[tuple[date, str], ...]]:
    return st.session_state.setdefault("event_phase_values", {})


def _render_phase_editor(
    event_id: str,
    event_start: date,
    event_end: date,
    peak_date: date,
) -> tuple[str, ...]:
    initial = _phase_values().get(
        event_id,
        auto_phase_assignments(event_start, event_end, peak_date=peak_date),
    )
    frame = pd.DataFrame(initial, columns=["Date", "Phase"])
    edited = st.data_editor(
        frame,
        hide_index=True,
        width="stretch",
        height=min(340, 38 * len(frame) + 38),
        key=f"event_phase_editor_{event_id}",
        column_config={
            "Date": st.column_config.DateColumn("Event day", disabled=True),
            "Phase": st.column_config.SelectboxColumn(
                "Phase classification",
                options=list(EVENT_PHASES),
                required=True,
            ),
        },
        disabled=["Date"],
    )
    assignments = tuple(
        (pd.Timestamp(row["Date"]).date(), str(row["Phase"]))
        for row in edited.to_dict("records")
    )
    _phase_values()[event_id] = assignments
    errors = validate_phase_assignments(assignments)
    _segment_timeline(
        event_start,
        event_end,
        segments_from_phase_assignments(assignments),
    )
    for error in errors:
        st.warning(error, icon=":material/warning:")
    return errors


def _render_split_controls(event_id: str, event_start: date, event_end: date) -> None:
    if event_start >= event_end:
        return
    split = DEMO_SPLIT_EVIDENCE.get(event_id)
    if split:
        st.markdown("#### Possible two-event structure")
        probability_columns = st.columns(2)
        probability_columns[0].metric(
            "One event", f"{split.same_event_probability:.0%}"
        )
        probability_columns[1].metric(
            "Two events", f"{split.separate_event_probability:.0%}"
        )
        st.caption(
            "These are demonstration values standing in for ERP's eventual "
            "same-episode and separate-episode probabilities."
        )

    midpoint = event_start + timedelta(days=(event_end - event_start).days // 2)
    if split:
        gap = (split.right_peak_date - split.left_peak_date).days
        default_cut = split.left_peak_date + timedelta(days=max(0, gap // 2))
        default_left_peak = split.left_peak_date
        default_right_peak = split.right_peak_date
    else:
        default_cut = min(midpoint, event_end - timedelta(days=1))
        default_left_peak = event_start + timedelta(
            days=(default_cut - event_start).days // 2
        )
        right_start = default_cut + timedelta(days=1)
        default_right_peak = right_start + timedelta(
            days=(event_end - right_start).days // 2
        )

    with st.expander("Split this occurrence into two", expanded=False):
        st.caption(
            "Choose the boundary and one provisional peak for each resulting occurrence. "
            "Production ERP evidence will supply these defaults later."
        )
        cut = st.date_input(
            "First occurrence ends",
            value=default_cut,
            min_value=event_start,
            max_value=event_end - timedelta(days=1),
            key=f"event_split_cut_{event_id}",
        )
        peak_columns = st.columns(2)
        left_peak = peak_columns[0].date_input(
            "First peak",
            value=default_left_peak,
            min_value=event_start,
            max_value=event_end,
            key=f"event_split_left_peak_{event_id}",
        )
        right_peak = peak_columns[1].date_input(
            "Second peak",
            value=default_right_peak,
            min_value=event_start,
            max_value=event_end,
            key=f"event_split_right_peak_{event_id}",
        )
        valid = event_start <= left_peak <= cut < right_peak <= event_end
        if not valid:
            st.warning(
                "The first peak must be on or before the boundary, and the second peak must be after it."
            )
        st.caption(
            f"Result: {event_start:%b %-d}–{cut:%b %-d} and "
            f"{cut + timedelta(days=1):%b %-d}–{event_end:%b %-d}."
        )
        if st.button(
            "Create two occurrences",
            type="primary",
            icon=":material/call_split:",
            use_container_width=True,
            disabled=not valid,
            key=f"accept_split_{event_id}",
        ):
            _split_decisions()[event_id] = {
                "left_peak_date": left_peak,
                "right_peak_date": right_peak,
                "cut_date": cut,
                "same_event_probability": (
                    split.same_event_probability if split else None
                ),
                "separate_event_probability": (
                    split.separate_event_probability if split else None
                ),
            }
            st.session_state.events_workspace_selected = f"{event_id}::a"
            st.rerun()


def _render_occurrence_editor(bundle, event: pd.Series) -> None:
    event_id = str(event["disturbance_id"])
    annotation = _annotation(event)
    event_start = pd.Timestamp(event["start_date"]).date()
    event_end = pd.Timestamp(event["end_date"]).date()
    parent_id = str(event.get("split_parent_id") or "")
    peak_value = event.get("erp_peak_date")
    peak_date = (
        pd.Timestamp(peak_value).date()
        if pd.notna(peak_value)
        else event_start + timedelta(days=((event_end - event_start).days // 2))
    )

    st.markdown('<div class="event-section-kicker">Selected occurrence</div>', unsafe_allow_html=True)
    title_column, evidence_column = st.columns([3, 1.4])
    with title_column:
        st.markdown(f"### {event_start:%B %-d}–{event_end:%B %-d, %Y}")
        st.caption(
            f"{'ERP-separated occurrence' if parent_id else 'One immutable model detection'}"
            f" · {int(event['duration_days'])} days"
        )
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
    st.markdown("#### Event morphology")
    st.caption(
        "The phases begin as automatic classifications. Reviewers can change any day, "
        "so buildup or tail may be absent and the crest may extend to an event boundary."
    )
    phase_errors = _render_phase_editor(
        event_id,
        event_start,
        event_end,
        peak_date,
    )
    st.session_state[f"event_phase_errors_{event_id}"] = phase_errors

    if parent_id:
        if st.button(
            "Restore the original occurrence",
            icon=":material/undo:",
            key=f"undo_split_{event_id}",
        ):
            _split_decisions().pop(parent_id, None)
            st.session_state.events_workspace_selected = parent_id
            st.rerun()
    else:
        _render_split_controls(event_id, event_start, event_end)

    st.divider()
    st.markdown("#### Related history")
    family = str(annotation["family"])
    prior = _family_history(family) if family != "Unassigned" else ()
    if prior and annotation["name"] != "Unidentified":
        columns = st.columns(min(3, len(prior)))
        for index, (column, occurrence) in enumerate(zip(columns, prior)):
            with column:
                if st.button(
                    f"**{occurrence['label']}**  \n"
                    f"{_signed_percent(float(occurrence['peak_percent']))} peak · "
                    f"{int(occurrence['duration_days'])} days",
                    use_container_width=True,
                    key=f"related_history_{event_id}_{index}",
                ):
                    st.session_state[f"related_history_selected_{event_id}"] = index
                    st.rerun()
        selected_history = st.session_state.get(f"related_history_selected_{event_id}")
        if selected_history is not None and selected_history < len(prior):
            occurrence = prior[selected_history]
            st.info(
                f"Comparing with {occurrence['label']}: "
                f"{_signed_percent(float(occurrence['peak_percent']))} peak over "
                f"{int(occurrence['duration_days'])} days."
            )
    else:
        st.info(
            "Related history appears after this occurrence is named and saved to an event family."
        )


def _render_details(event: pd.Series) -> None:
    event_id = str(event["disturbance_id"])
    annotation = _annotation(event)
    metadata = DEFAULT_EVENT_METADATA.get(event_id, {})
    families = sorted(set(_catalog_names("families")) | {str(annotation["family"]), "Unassigned"})
    categories = sorted(
        set(_catalog_names("categories")) | {str(annotation["category"]), "Unclassified"}
    )
    with st.container(border=True):
        st.markdown('<div class="event-section-kicker">Manager interpretation</div>', unsafe_allow_html=True)
        st.markdown("### Describe this occurrence")
        name = st.text_input(
            "Occurrence name",
            value=str(annotation["name"]),
            key=f"event_name_{event_id}",
        )
        suggested_name = str(annotation.get("suggested_name", "Unidentified"))
        if annotation["name"] == "Unidentified" and suggested_name != "Unidentified":
            st.caption(f"Model-assisted suggestion: {suggested_name}")
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
            categories,
            index=categories.index(str(annotation["category"])),
            key=f"event_category_{event_id}",
        )
        status = st.selectbox(
            "Review status",
            STATUSES,
            index=STATUSES.index(str(annotation["status"])),
            key=f"event_status_{event_id}",
        )
        with st.expander("Create a new tag"):
            new_tag = st.text_input(
                "Tag name",
                placeholder="For example: construction",
                key=f"new_event_tag_{event_id}",
            )
            if st.button(
                "Create and add tag",
                key=f"add_event_tag_{event_id}",
                disabled=not new_tag.strip(),
            ):
                clean_tag = _slug(new_tag)
                _create_catalog_item("tags", clean_tag)
                selected_tags = list(
                    st.session_state.get(f"event_tags_{event_id}", annotation["tags"])
                )
                if clean_tag and clean_tag not in selected_tags:
                    selected_tags.append(clean_tag)
                st.session_state[f"event_tags_{event_id}"] = selected_tags
        tag_options = sorted(set(_catalog_names("tags")) | set(annotation["tags"]))
        tag_key = f"event_tags_{event_id}"
        tag_arguments = {"key": tag_key}
        if tag_key not in st.session_state:
            tag_arguments["default"] = list(annotation["tags"])
        tags = st.multiselect("Tags", tag_options, **tag_arguments)
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
        phase_errors = tuple(
            st.session_state.get(f"event_phase_errors_{event_id}", ())
        )
        if st.button(
            "Save review",
            type="primary",
            use_container_width=True,
            disabled=bool(phase_errors),
            key=f"save_event_review_{event_id}",
        ):
            _records()[event_id] = {
                "name": name.strip() or "Unidentified",
                "suggested_name": str(metadata.get("suggested_name", "Unidentified")),
                "family": family,
                "category": category,
                "status": status,
                "tags": tuple(tags),
                "notes": notes,
                "phases": _phase_values().get(event_id, ()),
            }
            st.session_state["event_review_saved_notice"] = True
            st.rerun()


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
        ("All categories", "Unclassified") + tuple(_catalog_names("categories")),
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
            rows = grouped[selected_family] + list(_family_history(selected_family))
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


def _render_taxonomy_group(kind: str, singular: str) -> None:
    catalog = _taxonomy()[kind]
    st.markdown(f"### {singular.title()} management")
    if kind == "tags":
        st.caption(
            "Tags are lightweight search labels. Anyone reviewing an occurrence can create one inline."
        )
    else:
        st.caption(
            f"{singular.title()} changes are deliberate because they alter how occurrences are organized and pooled."
        )

    with st.form(f"create_{kind}", border=True):
        new_name = st.text_input(f"New {singular} name")
        new_description = st.text_input("Description", placeholder="What should this be used for?")
        submitted = st.form_submit_button(
            f"Create {singular}", type="primary", use_container_width=True
        )
        if submitted:
            normalized = _slug(new_name) if kind == "tags" else new_name
            if _create_catalog_item(kind, normalized, new_description):
                st.session_state[f"taxonomy_selected_{kind}"] = " ".join(normalized.split())
                st.rerun()
            else:
                st.warning(f"Enter a new, unique {singular} name.")

    if not catalog:
        st.info(f"No {kind} have been created.")
        return
    names = sorted(catalog)
    selected = st.session_state.get(f"taxonomy_selected_{kind}", names[0])
    if selected not in names:
        selected = names[0]
    selected = st.selectbox(
        f"Select {singular}",
        names,
        index=names.index(selected),
        key=f"taxonomy_picker_{kind}",
        format_func=lambda name: f"{name}{' · archived' if not catalog[name].get('active', True) else ''}",
    )
    current = catalog[selected]
    edited_name = st.text_input(
        "Display name",
        value=selected,
        key=f"taxonomy_name_{kind}_{selected}",
    )
    edited_description = st.text_area(
        "Description",
        value=str(current.get("description", "")),
        key=f"taxonomy_description_{kind}_{selected}",
        height=90,
    )
    action_columns = st.columns(2)
    if action_columns[0].button(
        "Save changes",
        type="primary",
        use_container_width=True,
        key=f"save_taxonomy_{kind}_{selected}",
    ):
        normalized = _slug(edited_name) if kind == "tags" else edited_name
        if _rename_catalog_item(kind, selected, normalized, edited_description):
            st.session_state[f"taxonomy_selected_{kind}"] = " ".join(normalized.split())
            st.rerun()
        st.error("That name is empty or already in use.")
    active = bool(current.get("active", True))
    if action_columns[1].button(
        "Archive" if active else "Restore",
        use_container_width=True,
        key=f"archive_taxonomy_{kind}_{selected}",
    ):
        current["active"] = not active
        st.rerun()


def _render_taxonomy_manager() -> None:
    st.markdown("## Classification library")
    st.caption(
        "Manage the controlled vocabulary used during event review. Archiving preserves existing assignments."
    )
    family_tab, category_tab, tag_tab = st.tabs(["Families", "Categories", "Tags"])
    with family_tab:
        _render_taxonomy_group("families", "family")
    with category_tab:
        _render_taxonomy_group("categories", "category")
    with tag_tab:
        _render_taxonomy_group("tags", "tag")


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
        .event-phase-cell.crest { background: #f5e6bd; color: #846a22; }
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
    if st.session_state.pop("event_review_saved_notice", False):
        st.toast("Event review saved for this session.", icon="✅")
    events = _event_frame(bundle)
    view = st.segmented_control(
        "Events view",
        ["Occurrences", "Families", "Classifications"],
        default="Occurrences",
        label_visibility="collapsed",
        key="events_workspace_view",
    )
    if view == "Classifications":
        _render_taxonomy_manager()
    else:
        history = history_rows(bundle, RESTAURANT)
        timeline_min = pd.Timestamp(history["target_date"].min()).date()
        timeline_max = pd.Timestamp(history["target_date"].max()).date()
        preset = st.segmented_control(
            "Timeline",
            ["Last 30 days", "Last 90 days", "All history", "Custom"],
            default="Last 30 days",
            key="events_workspace_timeline_preset",
        )
        if preset == "Last 30 days":
            timeline_start = max(timeline_min, timeline_max - timedelta(days=29))
            timeline_end = timeline_max
        elif preset == "Last 90 days":
            timeline_start = max(timeline_min, timeline_max - timedelta(days=89))
            timeline_end = timeline_max
        elif preset == "All history":
            timeline_start, timeline_end = timeline_min, timeline_max
        else:
            chosen = st.date_input(
                "Visible timeline",
                value=(max(timeline_min, timeline_max - timedelta(days=29)), timeline_max),
                min_value=timeline_min,
                max_value=timeline_max,
                key="events_workspace_custom_timeline",
            )
            if isinstance(chosen, (tuple, list)) and len(chosen) == 2:
                timeline_start, timeline_end = chosen
            else:
                timeline_start = timeline_end = chosen[0] if isinstance(chosen, (tuple, list)) else chosen
        visible_events = events_in_window(
            events,
            start_date=timeline_start,
            end_date=timeline_end,
        )
        st.caption(
            f"Showing detected occurrences overlapping {timeline_start:%b %-d, %Y}–"
            f"{timeline_end:%b %-d, %Y}. Family history outside this timeline appears only when reviewing a linked occurrence."
        )
        if view == "Families":
            _render_families(visible_events)
        else:
            _render_occurrences(bundle, visible_events)
    st.caption(
        "MVP annotations are retained only for this Streamlit session. Model detections are read-only."
    )
