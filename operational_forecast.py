"""Downtown operational-forecast MVP with a document-backed marketing draft."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from dashboard_charts import forecast_replay_chart
from dashboard_data import (
    disturbance_rows,
    forecast_replay_history,
    forecast_rows,
    history_rows,
    recent_rows,
)
from event_planning import (
    EventFamilyAnalysis,
    EventPlacement,
    EventScenarioResult,
    event_catalog,
    event_source_effect,
    event_uncertainty,
    family_analysis_by_template,
    make_placement,
    placements_equivalent,
    recurring_starts,
    replace_placement,
    resample_effect,
    score_event_plan,
    template_by_id,
)
from events_workspace import render_events_workspace
from marketing_planning import (
    CHANNEL_ORDER,
    CHANNEL_SPECS,
    MarketingPlan,
    MarketingPlanState,
    ScenarioResult,
    SpendBlock,
    build_spend_blocks,
    implement_plan,
    initialize_draft,
    load_plan_state,
    make_plan,
    pause_days_by_active_block,
    plans_equivalent,
    reflow_spend_blocks,
    saturation_thresholds,
    save_draft,
    save_plan_state,
    score_plan,
    zero_plan,
)


ROOT = Path(__file__).resolve().parent
PLAN_STATE_PATH = ROOT / "data" / "marketing_plan_state_v1.json"
BASELINE_VINTAGE = "forecast-2-dashboard-demo-v1"
MMM_FIT_VERSION = "frozen-mmm-preview-v1"
RESTAURANT = "downtown"
PAGE_FORECAST = "Forecast"
PAGE_REPLAY = "Forecast replay"
PAGE_EVENTS = "Events"


def _money(value: float, *, signed: bool = False) -> str:
    sign = "+" if signed and value > 0 else "−" if signed and value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _shifted_baseline(bundle) -> pd.DataFrame:
    """Project the published lead-day posterior onto the current MVP horizon."""

    frame = forecast_rows(bundle, RESTAURANT).copy().reset_index(drop=True)
    start = pd.Timestamp(date.today() + timedelta(days=1))
    frame["target_date"] = pd.date_range(start, periods=len(frame), freq="D")
    return frame


def _zero_for_baseline(baseline: pd.DataFrame) -> MarketingPlan:
    return zero_plan(
        restaurant=RESTAURANT,
        start_date=baseline["target_date"].min().date(),
        days=14,
        baseline_vintage=BASELINE_VINTAGE,
        mmm_fit_version=MMM_FIT_VERSION,
    )


def _plan_state() -> MarketingPlanState:
    return load_plan_state(PLAN_STATE_PATH)


def _published_plan(state: MarketingPlanState, baseline: pd.DataFrame) -> MarketingPlan:
    return state.published_plan or _zero_for_baseline(baseline)


def _clear_working_draft() -> None:
    for key in (
        "marketing_working_plan",
        "marketing_clean_reference",
        "marketing_dirty",
        "marketing_drawer_open",
        "marketing_exit_requested",
    ):
        st.session_state.pop(key, None)
    st.session_state.marketing_clear_widget_state = True


def _clear_event_working_draft() -> None:
    for key in (
        "event_working_placements",
        "event_clean_reference",
        "event_dirty",
        "event_drawer_open",
        "event_exit_requested",
        "event_editing_placement_id",
        "event_pending_edit_placement_id",
    ):
        st.session_state.pop(key, None)


def _published_event_placements() -> tuple[EventPlacement, ...]:
    return tuple(st.session_state.get("event_published_placements", ()))


def _begin_event_draft() -> None:
    if "event_working_placements" not in st.session_state:
        source = st.session_state.get(
            "event_saved_draft", _published_event_placements()
        )
        working = tuple(source)
        st.session_state.event_working_placements = working
        st.session_state.event_clean_reference = working
        st.session_state.event_dirty = False
    st.session_state.event_draft_mode = True
    st.session_state.event_drawer_open = True


def _working_event_placements() -> tuple[EventPlacement, ...]:
    if "event_working_placements" not in st.session_state:
        _begin_event_draft()
    return tuple(st.session_state.event_working_placements)


def _save_event_draft_to_session() -> None:
    placements = _working_event_placements()
    st.session_state.event_saved_draft = placements
    st.session_state.event_clean_reference = placements
    st.session_state.event_dirty = False


def _has_unsaved_changes() -> bool:
    return bool(st.session_state.get("marketing_dirty")) or bool(
        st.session_state.get("event_dirty")
    )


def _event_template_changed() -> None:
    """A direct catalog choice starts a new placement instead of editing one."""

    st.session_state.pop("event_editing_placement_id", None)


def _clear_event_day_shape(template_id: str) -> None:
    prefix = f"event_day_{template_id}_"
    for key in list(st.session_state):
        if str(key).startswith(prefix):
            st.session_state.pop(key, None)


def _event_shape_source_changed(template_id: str) -> None:
    """Start the selected source cleanly without stale per-day edits."""

    _clear_event_day_shape(template_id)
    source = st.session_state.get(
        f"event_shape_source_{template_id}", "Family estimate"
    )
    st.session_state[f"event_edit_shape_{template_id}"] = source == "Start blank"


def _reset_event_family_shape(template_id: str) -> None:
    _clear_event_day_shape(template_id)
    st.session_state[f"event_shape_source_{template_id}"] = "Family estimate"
    st.session_state[f"event_magnitude_{template_id}"] = 100
    st.session_state[f"event_edit_shape_{template_id}"] = False


def _apply_pending_event_edit(
    placements: tuple[EventPlacement, ...],
) -> EventPlacement | None:
    """Load a selected timeline card into controls before widgets render."""

    pending_id = st.session_state.pop("event_pending_edit_placement_id", None)
    if pending_id is None:
        editing_id = st.session_state.get("event_editing_placement_id")
        return next(
            (item for item in placements if item.placement_id == editing_id),
            None,
        )
    placement = next(
        (item for item in placements if item.placement_id == pending_id),
        None,
    )
    if placement is None:
        st.session_state.pop("event_editing_placement_id", None)
        return None

    template_id = placement.template_id
    duration = len(placement.daily_effect)
    st.session_state.event_editing_placement_id = placement.placement_id
    st.session_state.event_catalog_view = "Browse all"
    st.session_state.event_catalog_search = ""
    st.session_state.event_category_filter = None
    st.session_state.event_tag_filter = []
    st.session_state.event_template_id = template_id
    st.session_state[f"event_occurrence_{template_id}"] = "One time"
    st.session_state[f"event_start_{template_id}"] = placement.start_date
    st.session_state[f"event_duration_{template_id}"] = duration
    st.session_state[f"event_magnitude_{template_id}"] = 100
    st.session_state[f"event_edit_shape_{template_id}"] = True
    for day_index, value in enumerate(placement.daily_effect, start=1):
        st.session_state[
            f"event_day_{template_id}_{duration}_{day_index}"
        ] = int(round(100 * value))
    return placement


def _apply_pending_widget_clear() -> None:
    if not st.session_state.pop("marketing_clear_widget_state", False):
        return
    widget_keys = (
        "marketing_start_date",
        "marketing_period",
        "marketing_end_date",
        "marketing_total_budget",
        "marketing_allocation_mode",
        "marketing_optimization_goal",
        "marketing_cadence_days",
        "marketing_selected_block",
        "marketing_selected_block_index",
        "marketing_editing_block_index",
    )
    for key in widget_keys:
        st.session_state.pop(key, None)
    for channel in CHANNEL_ORDER:
        st.session_state.pop(f"marketing_eligible_{channel}", None)
        st.session_state.pop(f"marketing_manual_{channel}", None)
    for key in list(st.session_state):
        if key.startswith("marketing_block_") or key.startswith(
            "marketing_allow_higher_"
        ) or key.startswith(
            "marketing_pause_after_"
        ):
            st.session_state.pop(key, None)


def _allocation_mode_changed() -> None:
    """Carry the current plan smoothly between optimized and manual editing."""

    mode = st.session_state.get("marketing_allocation_mode")
    if mode == "Manual schedule":
        # Seed manual block controls from the latest optimized working schedule.
        for key in list(st.session_state):
            if key.startswith("marketing_block_") or key.startswith(
                "marketing_allow_higher_"
            ):
                st.session_state.pop(key, None)
        st.session_state.pop("marketing_editing_block_index", None)
        return
    if mode == "Optimize schedule":
        st.session_state.pop("marketing_editing_block_index", None)
        # The optimizer starts from the total of the latest manual working plan.
        working = st.session_state.get("marketing_working_plan")
        manual_total = float(working.total_budget) if working is not None else 0.0
        st.session_state["marketing_total_budget"] = int(
            round(manual_total / 25.0) * 25
        )


def _switch_to_manual_schedule(selected_index: int) -> None:
    st.session_state["marketing_allocation_mode"] = "Manual schedule"
    st.session_state["marketing_editing_block_index"] = int(selected_index)
    st.session_state["marketing_selected_block_index"] = int(selected_index)
    for key in list(st.session_state):
        if key.startswith("marketing_block_") or key.startswith(
            "marketing_allow_higher_"
        ):
            st.session_state.pop(key, None)


def _edit_selected_spend_block(selected_index: int) -> None:
    st.session_state["marketing_editing_block_index"] = int(selected_index)
    st.session_state["marketing_selected_block_index"] = int(selected_index)


def _selected_spend_block_changed() -> None:
    """Keep the open manual editor attached to the visibly selected block."""

    if "marketing_editing_block_index" in st.session_state:
        st.session_state["marketing_editing_block_index"] = int(
            st.session_state["marketing_selected_block_index"]
        )


def _close_spend_block_editor() -> None:
    st.session_state.pop("marketing_editing_block_index", None)


def _sync_extended_amount_to_slider(
    amount_key: str, slider_key: str, maximum: int
) -> None:
    amount = max(0.0, float(st.session_state.get(amount_key, 0.0)))
    st.session_state[slider_key] = min(int(round(amount / 5.0) * 5), maximum)


def _sync_extended_slider_to_amount(slider_key: str, amount_key: str) -> None:
    st.session_state[amount_key] = float(st.session_state.get(slider_key, 0.0))


def _begin_working_draft(state: MarketingPlanState, baseline: pd.DataFrame) -> None:
    if "marketing_working_plan" not in st.session_state:
        plan = initialize_draft(
            state,
            restaurant=RESTAURANT,
            start_date=baseline["target_date"].min().date(),
            baseline_vintage=BASELINE_VINTAGE,
            mmm_fit_version=MMM_FIT_VERSION,
        )
        st.session_state.marketing_working_plan = plan
        st.session_state.marketing_clean_reference = plan
        st.session_state.marketing_dirty = False
    st.session_state.marketing_draft_mode = True
    st.session_state.marketing_drawer_open = True


def _working_plan(state: MarketingPlanState, baseline: pd.DataFrame) -> MarketingPlan:
    if "marketing_working_plan" not in st.session_state:
        plan = initialize_draft(
            state,
            restaurant=RESTAURANT,
            start_date=baseline["target_date"].min().date(),
            baseline_vintage=BASELINE_VINTAGE,
            mmm_fit_version=MMM_FIT_VERSION,
        )
        st.session_state.marketing_working_plan = plan
        st.session_state.marketing_clean_reference = plan
        st.session_state.marketing_dirty = False
    return st.session_state.marketing_working_plan


def _browser_leave_guard(enabled: bool) -> None:
    """Use the browser's standard generic warning for refresh/tab-close events."""

    flag = "true" if enabled else "false"
    st.html(
        f"""
        <script>
        (() => {{
          const host = window;
          if (host.__marketingDraftBeforeUnload) {{
            host.removeEventListener('beforeunload', host.__marketingDraftBeforeUnload);
            delete host.__marketingDraftBeforeUnload;
          }}
          if ({flag}) {{
            const handler = (event) => {{
              event.preventDefault();
              event.returnValue = '';
            }};
            host.__marketingDraftBeforeUnload = handler;
            host.addEventListener('beforeunload', handler);
          }}
        }})();
        </script>
        """,
        unsafe_allow_javascript=True,
    )


def _complete_navigation(target: dict[str, str]) -> None:
    if target["kind"] == "restaurant":
        restaurant = target["value"]
        st.session_state.active_restaurant = restaurant
        st.session_state.restaurant_picker_nonce = (
            int(st.session_state.get("restaurant_picker_nonce", 0)) + 1
        )
    else:
        page = target["value"]
        st.session_state.downtown_nav_active = page
        st.session_state.downtown_nav_nonce = (
            int(st.session_state.get("downtown_nav_nonce", 0)) + 1
        )
    st.session_state.pop("downtown_pending_nav", None)


@st.dialog("Unsaved draft changes", dismissible=False)
def _navigation_guard() -> None:
    target = st.session_state.get(
        "downtown_pending_nav", {"kind": "page", "value": PAGE_FORECAST}
    )
    st.write(
        "Your preview contains changes that have not been saved. Save them, "
        "discard them, or stay on the forecast."
    )
    if st.button("Stay and review", type="primary", use_container_width=True):
        if target["kind"] == "restaurant":
            st.session_state.restaurant_picker_nonce = (
                int(st.session_state.get("restaurant_picker_nonce", 0)) + 1
            )
        else:
            st.session_state.downtown_nav_nonce = (
                int(st.session_state.get("downtown_nav_nonce", 0)) + 1
            )
        st.session_state.pop("downtown_pending_nav", None)
        st.rerun()
    if st.button("Save draft and continue", use_container_width=True):
        if bool(st.session_state.get("marketing_dirty")):
            state = _plan_state()
            plan = st.session_state.marketing_working_plan
            save_plan_state(PLAN_STATE_PATH, save_draft(state, plan))
            _clear_working_draft()
            st.session_state.marketing_draft_mode = False
        if bool(st.session_state.get("event_dirty")):
            _save_event_draft_to_session()
            _clear_event_working_draft()
            st.session_state.event_draft_mode = False
        _complete_navigation(target)
        st.rerun()
    if st.button("Discard changes and continue", use_container_width=True):
        if bool(st.session_state.get("marketing_dirty")):
            _clear_working_draft()
        if bool(st.session_state.get("event_dirty")):
            _clear_event_working_draft()
        st.session_state.marketing_draft_mode = False
        st.session_state.event_draft_mode = False
        _complete_navigation(target)
        st.rerun()


def _sidebar_navigation() -> str:
    if "downtown_nav_active" not in st.session_state:
        st.session_state.downtown_nav_active = PAGE_FORECAST
    active = st.session_state.downtown_nav_active
    nonce = int(st.session_state.get("downtown_nav_nonce", 0))
    pending = st.session_state.get("downtown_pending_nav")
    if pending and pending.get("kind") == "restaurant":
        _navigation_guard()
    with st.sidebar:
        st.markdown("### Workspace")
        requested = st.radio(
            "Workspace",
            [PAGE_FORECAST, PAGE_EVENTS, PAGE_REPLAY],
            index=[PAGE_FORECAST, PAGE_EVENTS, PAGE_REPLAY].index(active),
            label_visibility="collapsed",
            key=f"downtown_nav_picker_{nonce}",
        )
        st.button("Marketing", disabled=True, use_container_width=True)
        st.caption("The dedicated marketing workspace arrives after this MVP slice.")
    if requested != active:
        if _has_unsaved_changes():
            st.session_state.downtown_pending_nav = {
                "kind": "page",
                "value": requested,
            }
            _navigation_guard()
            return active
        st.session_state.downtown_nav_active = requested
        return requested
    return active


def _operational_chart(
    published: EventScenarioResult,
    *,
    draft: EventScenarioResult | None,
    actual_value: float | None,
) -> alt.Chart:
    daily = published.daily.copy()
    daily["series_date"] = daily["target_date"]
    base = alt.Chart(daily).encode(
        x=alt.X("series_date:T", title=None, axis=alt.Axis(format="%b %-d", tickCount=7)),
    )
    band = base.mark_area(color="#8ba797", opacity=0.20).encode(
        y=alt.Y(
            "event_operational_q05:Q",
            title="Sales",
            axis=alt.Axis(format="$,.0f"),
            scale=alt.Scale(zero=False),
        ),
        y2="event_operational_q95:Q",
    )
    organic = base.mark_line(color="#6f8790", strokeDash=[6, 5], strokeWidth=2).encode(
        y=alt.Y("median:Q", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("series_date:T", title="Date", format="%A, %b %-d"),
            alt.Tooltip("median:Q", title="Organic baseline", format="$,.0f"),
        ],
    )
    published_line = base.mark_line(color="#2f6b55", strokeWidth=3, point=True).encode(
        y=alt.Y("event_operational_median:Q", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("series_date:T", title="Date", format="%A, %b %-d"),
            alt.Tooltip("event_operational_median:Q", title="Implemented forecast", format="$,.0f"),
            alt.Tooltip("marketing_lift:Q", title="Marketing lift", format="$,.0f"),
            alt.Tooltip("event_lift:Q", title="Event effect", format="$,.0f"),
        ],
    )
    layers: list[alt.Chart] = [band, organic, published_line]
    if draft is not None:
        comparison = daily[["series_date", "event_operational_median"]].rename(
            columns={"event_operational_median": "published_forecast"}
        )
        comparison["draft_forecast"] = draft.daily[
            "event_operational_median"
        ].to_numpy()
        comparison["draft_q05"] = draft.daily[
            "event_operational_q05"
        ].to_numpy()
        comparison["draft_q95"] = draft.daily[
            "event_operational_q95"
        ].to_numpy()
        draft_band = alt.Chart(comparison).mark_area(
            color="#d96a47", opacity=0.10
        ).encode(
            x=alt.X("series_date:T", title=None),
            y=alt.Y(
                "draft_q05:Q",
                title="Sales",
                axis=alt.Axis(format="$,.0f"),
                scale=alt.Scale(zero=False),
            ),
            y2="draft_q95:Q",
        )
        gap = alt.Chart(comparison).mark_area(color="#d96a47", opacity=0.16).encode(
            x=alt.X("series_date:T", title=None),
            y=alt.Y(
                "published_forecast:Q",
                title="Sales",
                axis=alt.Axis(format="$,.0f"),
                scale=alt.Scale(zero=False),
            ),
            y2="draft_forecast:Q",
        )
        draft_line = alt.Chart(comparison).mark_line(
            color="#d96a47", strokeWidth=3, point=True
        ).encode(
            x=alt.X("series_date:T", title=None),
            y=alt.Y("draft_forecast:Q", scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("series_date:T", title="Date", format="%A, %b %-d"),
                alt.Tooltip("published_forecast:Q", title="Implemented", format="$,.0f"),
                alt.Tooltip("draft_forecast:Q", title="Draft", format="$,.0f"),
            ],
        )
        layers.extend([draft_band, gap, draft_line])
    if actual_value is not None:
        actual = pd.DataFrame(
            {
                "series_date": [daily["series_date"].min() - pd.Timedelta(days=1)],
                "actual_sales": [actual_value],
            }
        )
        actual_point = alt.Chart(actual).mark_point(
            color="#d96a47", filled=True, size=75
        ).encode(
            x=alt.X("series_date:T", title=None),
            y=alt.Y(
                "actual_sales:Q",
                title="Sales",
                axis=alt.Axis(format="$,.0f"),
                scale=alt.Scale(zero=False),
            ),
            tooltip=[
                alt.Tooltip("series_date:T", title="Latest actual", format="%A, %b %-d"),
                alt.Tooltip("actual_sales:Q", title="Sales", format="$,.0f"),
            ],
        )
        layers.append(actual_point)
    return (
        alt.layer(*layers)
        .properties(height=355)
        .configure_view(strokeOpacity=0)
        .configure_axis(gridColor="#e3ded3", labelColor="#66736c", titleColor="#66736c")
    )


def _saturation_track(
    channel: str,
    duration_days: int,
    maximum: int,
    widget_key: str,
    *,
    show_thumb_value: bool = True,
) -> None:
    yellow, red = saturation_thresholds(channel, duration_days)
    yellow_pct = min(100.0, yellow / maximum * 100.0)
    red_pct = min(100.0, red / maximum * 100.0)
    thumb_value_style = "" if show_thumb_value else "display: none !important;"
    st.html(
        f"""
        <style>
          .st-key-{widget_key} [role="group"] > div > div:first-child {{
            background: linear-gradient(90deg,
            #8eb59e 0%, #8eb59e {yellow_pct:.1f}%,
            #e2bc62 {yellow_pct:.1f}%, #e2bc62 {red_pct:.1f}%,
            #d86d53 {red_pct:.1f}%, #d86d53 100%) !important;
            opacity: 1 !important;
          }}
          .st-key-{widget_key} [role="slider"] {{
            width: 18px !important; height: 18px !important;
            border: 2px solid #f7f4ec !important;
            box-shadow: 0 0 0 1px #2f6b55 !important;
          }}
          .st-key-{widget_key} [data-testid="stSliderThumbValue"] {{
            {thumb_value_style}
          }}
          .st-key-{widget_key} [data-testid="stSliderTickBar"] {{
            display: none !important;
          }}
        </style>
        <div class="saturation-labels slider-saturation-labels">
          <span>efficient</span><span>diminishing</span><span>saturated</span>
        </div>
        """
    )


def _candidate_from_controls(plan: MarketingPlan) -> MarketingPlan | None:
    period_default = (
        "7 days" if plan.duration_days == 7 else "14 days" if plan.duration_days == 14 else "Custom"
    )
    period = st.segmented_control(
        "Plan period",
        options=["7 days", "14 days", "Custom"],
        default=period_default,
        key="marketing_period",
        width="stretch",
    ) or period_default
    if period == "Custom":
        date_columns = st.columns(2)
        start = date_columns[0].date_input(
            "Start date",
            value=max(plan.start, date.today()),
            min_value=date.today(),
            key="marketing_start_date",
        )
        end = date_columns[1].date_input(
            "End date",
            value=max(plan.end, start),
            min_value=start,
            key="marketing_end_date",
        )
    else:
        start = st.date_input(
            "Plan starts",
            value=max(plan.start, date.today()),
            min_value=date.today(),
            key="marketing_start_date",
        )
    if period == "7 days":
        end = start + timedelta(days=6)
    elif period == "14 days":
        end = start + timedelta(days=13)
    duration = (end - start).days + 1
    st.caption(
        f"{start:%b %-d}–{end:%b %-d} · spending changes forecast values, not the 14-day horizon"
    )
    default_mode_label = (
        "Optimize schedule" if plan.allocation_mode == "optimized" else "Manual schedule"
    )
    mode_defaults = {}
    if "marketing_allocation_mode" not in st.session_state:
        mode_defaults["default"] = default_mode_label
    mode_label = st.segmented_control(
        "Plan construction",
        ["Optimize schedule", "Manual schedule"],
        key="marketing_allocation_mode",
        on_change=_allocation_mode_changed,
        width="stretch",
        **mode_defaults,
    ) or st.session_state.get("marketing_allocation_mode", default_mode_label)
    mode = "optimized" if mode_label == "Optimize schedule" else "manual"

    goal_labels = {
        "Sales": "sales",
        "Balanced": "balanced",
        "Learn faster": "learning",
    }
    reverse_goal_labels = {value: key for key, value in goal_labels.items()}
    if mode == "optimized":
        goal_label = st.segmented_control(
            "Optimization goal",
            list(goal_labels),
            default=reverse_goal_labels.get(plan.optimization_goal, "Balanced"),
            key="marketing_optimization_goal",
            width="stretch",
        ) or "Balanced"
        optimization_goal = goal_labels[goal_label]
    else:
        optimization_goal = plan.optimization_goal

    cadence = int(
        st.number_input(
            "Days per learning block",
            min_value=2,
            max_value=7,
            value=int(plan.cadence_days),
            step=1,
            key="marketing_cadence_days",
        )
    )
    base_block_count = (duration + cadence - 1) // cadence
    maximum_pause_days = max(0, duration - base_block_count)
    saved_pauses = pause_days_by_active_block(plan.spend_blocks)
    pause_after: dict[int, int] = {}
    pause_capacity = maximum_pause_days
    for active_index in range(base_block_count):
        pause_key = f"marketing_pause_after_{active_index}"
        requested = int(
            st.session_state.get(
                pause_key, saved_pauses.get(active_index, 0)
            )
        )
        accepted = min(max(0, requested), pause_capacity)
        if pause_key in st.session_state and requested != accepted:
            st.session_state[pause_key] = accepted
        if accepted:
            pause_after[active_index] = accepted
            pause_capacity -= accepted
    budget_slot = st.empty()
    if mode == "optimized":
        budget_value = {}
        if "marketing_total_budget" not in st.session_state:
            budget_value["value"] = int(round(plan.total_budget / 25.0) * 25)
        total_budget = budget_slot.slider(
            "Total planned spend",
            min_value=0,
            max_value=15_000,
            step=25,
            format="$%d",
            key="marketing_total_budget",
            **budget_value,
        )
    else:
        total_budget = float(plan.total_budget)

    st.markdown("**Eligible channels**")
    eligibility_columns = st.columns(3)
    eligible: dict[str, bool] = {}
    for column, key in zip(eligibility_columns, CHANNEL_ORDER):
        eligible[key] = column.toggle(
            CHANNEL_SPECS[key].label,
            value=plan.channels[key].eligible,
            key=f"marketing_eligible_{key}",
        )
    if mode == "optimized":
        st.caption(
            "Changing channel eligibility re-optimizes every spend block while preserving the total budget."
        )
    else:
        st.caption(
            r"Manual blocks are not rebalanced automatically. Disabling a channel sets its spend to \$0 across the schedule. Switching back to Optimize schedule replaces manual block edits."
        )

    if mode == "optimized":
        if total_budget > 0 and not any(eligible.values()):
            st.error("Turn on at least one channel before allocating a positive budget.")
            return None
        candidate = make_plan(
            restaurant=RESTAURANT,
            start_date=start,
            end_date=end,
            total_budget=float(total_budget),
            eligible=eligible,
            allocation_mode=mode,
            allocations=None,
            baseline_vintage=BASELINE_VINTAGE,
            mmm_fit_version=MMM_FIT_VERSION,
            plan_id=plan.plan_id,
            optimization_goal=optimization_goal,
            cadence_days=cadence,
            pause_after=pause_after,
        )
        blocks = candidate.spend_blocks
    else:
        same_shape = (
            bool(plan.spend_blocks)
            and plan.start == start
            and plan.end == end
            and plan.cadence_days == cadence
        )
        source_blocks = (
            plan.spend_blocks
            if same_shape
            else build_spend_blocks(
                start_date=start,
                end_date=end,
                channel_totals={
                    key: plan.channels[key].amount if eligible[key] else 0.0
                    for key in CHANNEL_ORDER
                },
                cadence_days=cadence,
                optimization_goal=optimization_goal,
            )
        )
        blocks = reflow_spend_blocks(
            source_blocks,
            start_date=start,
            end_date=end,
            cadence_days=cadence,
            pause_after=pause_after,
        )

        # Reflect any saved widget edits in the schedule summary before rendering it.
        # Streamlit reruns top-to-bottom, so the preview rows must read the current
        # widget state rather than the original draft blocks.
        current_blocks: list[SpendBlock] = []
        active_index = -1
        for block in blocks:
            if block.is_pause:
                current_blocks.append(block)
                continue
            active_index += 1
            block_allows_higher = bool(
                st.session_state.get(
                    f"marketing_allow_higher_{active_index}", False
                )
            )
            daily_spend: dict[str, float] = {}
            for key in CHANNEL_ORDER:
                slider_key = f"marketing_block_{active_index}_{key}"
                extended_key = f"{slider_key}_extended"
                if not eligible[key]:
                    value = 0.0
                elif block_allows_higher and extended_key in st.session_state:
                    value = float(st.session_state[extended_key])
                elif slider_key in st.session_state:
                    value = float(st.session_state[slider_key])
                else:
                    value = float(block.daily_spend[key])
                daily_spend[key] = value
            current_blocks.append(
                SpendBlock(
                    start_date=block.start_date,
                    end_date=block.end_date,
                    daily_spend=daily_spend,
                    locked=block.locked,
                    is_pause=False,
                )
            )
        blocks = tuple(current_blocks)

    active_block_indices = [
        index for index, block in enumerate(blocks) if not block.is_pause
    ]
    actual_pauses = pause_days_by_active_block(blocks)

    def block_label(active_index: int) -> str:
        block = blocks[active_block_indices[active_index]]
        if mode == "manual":
            return f"{block.start:%b %-d}–{block.end:%b %-d}"
        daily_total = sum(block.daily_spend.values())
        return (
            f"{block.start:%b %-d}–{block.end:%b %-d} · "
            f"{_money(daily_total)}/day"
        )

    block_captions = []
    for active_index, physical_index in enumerate(active_block_indices):
        block = blocks[physical_index]
        caption = " · ".join(
            f"{CHANNEL_SPECS[key].label} {_money(block.daily_spend[key])}"
            for key in CHANNEL_ORDER
        )
        pause_days = actual_pauses.get(active_index, 0)
        if pause_days:
            pause_block = blocks[physical_index + 1]
            caption += (
                f" · No spend {pause_block.start:%b %-d}–{pause_block.end:%b %-d}"
                f" ({pause_days} day{'s' if pause_days != 1 else ''})"
            )
        block_captions.append(caption.replace("$", r"\$"))
    editing_index = (
        st.session_state.get("marketing_editing_block_index")
        if mode == "manual"
        else None
    )
    if editing_index is not None and not 0 <= int(editing_index) < len(
        active_block_indices
    ):
        st.session_state.pop("marketing_editing_block_index", None)
        editing_index = None
    st.markdown("**Spend schedule**")
    selected_block = st.radio(
        "Spending schedule",
        options=list(range(len(active_block_indices))),
        format_func=block_label,
        captions=block_captions,
        key="marketing_selected_block_index",
        label_visibility="collapsed",
        width="stretch",
        on_change=_selected_spend_block_changed,
    )
    selected_index = int(selected_block)
    selected = blocks[active_block_indices[selected_index]]

    current_pause_days = actual_pauses.get(selected_index, 0)
    other_pause_days = sum(actual_pauses.values()) - current_pause_days
    selected_pause_limit = max(0, maximum_pause_days - other_pause_days)
    pause_key = f"marketing_pause_after_{selected_index}"
    pause_defaults = {}
    if pause_key not in st.session_state:
        pause_defaults["value"] = current_pause_days
    pause_label_column, pause_input_column = st.columns(
        [1, 0.32], vertical_alignment="center"
    )
    pause_label_column.markdown("**No-spend days after this block**")
    pause_input_column.number_input(
        "No-spend days after this block",
        min_value=0,
        max_value=selected_pause_limit,
        step=1,
        key=pause_key,
        label_visibility="collapsed",
        width=104,
        help="Shifts every later block without overlap.",
        **pause_defaults,
    )

    if mode == "optimized":
        st.caption(
            rf"Selected block: {selected.duration_days} active days. Pauses are fixed at \$0 while the optimizer reallocates the budget across the remaining days."
        )
        st.button(
            "Switch to manual editing",
            use_container_width=True,
            on_click=_switch_to_manual_schedule,
            args=(selected_index,),
        )
        return candidate

    if editing_index is None:
        st.button(
            "Edit selected block",
            use_container_width=True,
            on_click=_edit_selected_spend_block,
            args=(selected_index,),
        )
    else:
        selected_index = int(editing_index)
        selected = blocks[active_block_indices[selected_index]]
        st.markdown(
            f"**Edit {selected.start:%b %-d}–{selected.end:%b %-d}** · daily spend"
        )
        allow_key = f"marketing_allow_higher_{selected_index}"
        normal_limits = {
            key: int(
                round(CHANNEL_SPECS[key].daily_saturation * 1.18 * 1.20 / 5.0)
                * 5
            )
            for key in CHANNEL_ORDER
        }
        allow_defaults = {}
        if allow_key not in st.session_state:
            allow_defaults["value"] = any(
                selected.daily_spend[key] > normal_limits[key]
                for key in CHANNEL_ORDER
            )
        allow_higher = st.toggle(
            "Allow higher spending",
            key=allow_key,
            help="Keeps the saturation range visible while allowing direct dollar entry beyond it.",
            **allow_defaults,
        )

        for key in CHANNEL_ORDER:
            base_value = float(selected.daily_spend[key]) if eligible[key] else 0.0
            slider_key = f"marketing_block_{selected_index}_{key}"
            extended_key = f"marketing_block_{selected_index}_{key}_extended"
            label = f"{CHANNEL_SPECS[key].label} per day"
            label_column, range_column, value_column = st.columns(
                [0.9, 2.55, 0.85], vertical_alignment="center"
            )
            label_class = "channel-spend-label" if eligible[key] else "channel-spend-label disabled"
            label_column.markdown(
                f'<div class="{label_class}">{CHANNEL_SPECS[key].label}</div>',
                unsafe_allow_html=True,
            )
            if allow_higher:
                extended_defaults = {}
                if extended_key not in st.session_state:
                    extended_defaults["value"] = float(
                        st.session_state.get(slider_key, base_value)
                    )
                extended_slider_key = f"{slider_key}_extended_range"
                extended_slider_defaults = {}
                if extended_slider_key not in st.session_state:
                    extended_amount = float(
                        st.session_state.get(extended_key, base_value)
                    )
                    extended_slider_defaults["value"] = min(
                        int(round(extended_amount / 5.0) * 5), normal_limits[key]
                    )
                value_column.number_input(
                    label,
                    min_value=0.0,
                    step=5.0,
                    format="%.0f",
                    icon=":material/attach_money:",
                    width=96,
                    disabled=not eligible[key],
                    key=extended_key,
                    label_visibility="collapsed",
                    on_change=_sync_extended_amount_to_slider,
                    args=(extended_key, extended_slider_key, normal_limits[key]),
                    **extended_defaults,
                )
                with range_column:
                    st.slider(
                        f"{label} decision range",
                        min_value=0,
                        max_value=normal_limits[key],
                        step=5,
                        format="$%d",
                        disabled=not eligible[key],
                        key=extended_slider_key,
                        label_visibility="collapsed",
                        on_change=_sync_extended_slider_to_amount,
                        args=(extended_slider_key, extended_key),
                        **extended_slider_defaults,
                    )
                    _saturation_track(
                        key,
                        1,
                        normal_limits[key],
                        extended_slider_key,
                        show_thumb_value=False,
                    )
            else:
                slider_defaults = {}
                if slider_key not in st.session_state:
                    slider_defaults["value"] = min(
                        int(round(base_value / 5.0) * 5), normal_limits[key]
                    )
                range_column.slider(
                    label,
                    min_value=0,
                    max_value=normal_limits[key],
                    step=5,
                    format="$%d",
                    disabled=not eligible[key],
                    key=slider_key,
                    label_visibility="collapsed",
                    **slider_defaults,
                )
                with range_column:
                    _saturation_track(
                        key,
                        1,
                        normal_limits[key],
                        slider_key,
                    )
        if allow_higher:
            st.caption(
                "Dollar entry may exceed the displayed decision range. The marker pins at the red edge once spending is materially saturated."
            )
        st.button(
            "Save block",
            type="primary",
            use_container_width=True,
            on_click=_close_spend_block_editor,
        )

    edited_blocks: list[SpendBlock] = []
    active_index = -1
    for block in blocks:
        if block.is_pause:
            edited_blocks.append(block)
            continue
        active_index += 1
        daily_spend: dict[str, float] = {}
        block_allows_higher = bool(
            st.session_state.get(
                f"marketing_allow_higher_{active_index}", False
            )
        )
        for key in CHANNEL_ORDER:
            slider_key = f"marketing_block_{active_index}_{key}"
            extended_key = f"{slider_key}_extended"
            if not eligible[key]:
                value = 0.0
            elif block_allows_higher and extended_key in st.session_state:
                value = float(st.session_state[extended_key])
            elif slider_key in st.session_state:
                value = float(st.session_state[slider_key])
            else:
                value = float(block.daily_spend[key])
            daily_spend[key] = value
        edited_blocks.append(
            SpendBlock(
                start_date=block.start_date,
                end_date=block.end_date,
                daily_spend=daily_spend,
                locked=active_index == selected_index or block.locked,
                is_pause=False,
            )
        )
    total_budget = sum(
        sum(block.daily_spend.values()) * block.duration_days
        for block in edited_blocks
    )
    edited_active_blocks = [block for block in edited_blocks if not block.is_pause]
    edited_selected = edited_active_blocks[selected_index]
    selected_daily_total = sum(edited_selected.daily_spend.values())
    selected_mix = " · ".join(
        f"{CHANNEL_SPECS[key].label} {_money(edited_selected.daily_spend[key])}"
        for key in CHANNEL_ORDER
    )
    selected_summary = (
        f"Selected block total: {_money(selected_daily_total)}/day · {selected_mix}"
    )
    st.caption(selected_summary.replace("$", r"\$"))
    budget_slot.markdown(
        f"""
        <div class="manual-plan-total">
          <span>Total planned spend</span>
          <strong>{_money(total_budget)}</strong>
          <small>Calculated from the manual spend blocks</small>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return make_plan(
        restaurant=RESTAURANT,
        start_date=start,
        end_date=end,
        total_budget=float(total_budget),
        eligible=eligible,
        allocation_mode="manual",
        allocations=None,
        baseline_vintage=BASELINE_VINTAGE,
        mmm_fit_version=MMM_FIT_VERSION,
        plan_id=plan.plan_id,
        optimization_goal=optimization_goal,
        cadence_days=cadence,
        spend_blocks=edited_blocks,
    )


@st.dialog(
    "Edit marketing draft",
    width="small",
    dismissible=False,
    position="right",
)
def _marketing_drawer(baseline: pd.DataFrame) -> None:
    state = _plan_state()
    plan = _working_plan(state, baseline)
    if st.session_state.get("marketing_exit_requested"):
        st.warning("Choose what to do with your working draft before returning to the implemented forecast.")
    candidate = _candidate_from_controls(plan)
    if candidate is not None:
        st.session_state.marketing_working_plan = candidate
        clean = st.session_state.get("marketing_clean_reference", plan)
        st.session_state.marketing_dirty = not plans_equivalent(candidate, clean)
        result = score_plan(baseline, candidate)
        impact_columns = st.columns(2)
        impact_columns[0].metric("Expected lift", _money(result.summary.marketing_lift, signed=True))
        impact_columns[1].metric("Average ROAS", f"{result.summary.average_roas:.2f}×")
        learning_target = {
            "sales": "Sales-first",
            "balanced": "Balanced",
            "learning": "Learn faster",
        }[candidate.optimization_goal]
        st.caption(f"Learning target: **{learning_target}**")
        if candidate.allocation_mode == "manual":
            st.caption(
                "Manual changes are not yet dynamically scored for expected MMM learning."
            )
        if result.summary.carryover_beyond_horizon > 1:
            st.caption(
                f"Estimated carryover after the visible horizon: "
                f"{_money(result.summary.carryover_beyond_horizon)}."
            )
    st.divider()
    preview_column, save_column, discard_column = st.columns(3)
    if preview_column.button(
        "Preview",
        disabled=candidate is None,
        use_container_width=True,
    ):
        st.session_state.marketing_draft_mode = True
        st.session_state.marketing_drawer_open = False
        st.session_state.marketing_exit_requested = False
        st.rerun()
    if save_column.button(
        "Save draft", disabled=candidate is None, use_container_width=True
    ):
        updated = save_draft(state, candidate)
        save_plan_state(PLAN_STATE_PATH, updated)
        st.session_state.marketing_clean_reference = candidate
        st.session_state.marketing_dirty = False
        st.session_state.marketing_draft_mode = True
        st.session_state.marketing_drawer_open = False
        st.session_state.marketing_exit_requested = False
        st.rerun()
    if discard_column.button("Discard changes", use_container_width=True):
        _clear_working_draft()
        st.session_state.marketing_draft_mode = False
        st.rerun()
    if st.button(
        "Implement plan",
        type="primary",
        disabled=candidate is None,
        use_container_width=True,
    ):
        save_plan_state(PLAN_STATE_PATH, implement_plan(state, candidate))
        _clear_working_draft()
        st.session_state.marketing_draft_mode = False
        st.rerun()


def _event_family_chart(
    template_id: str,
    analysis: EventFamilyAnalysis,
) -> alt.Chart:
    template = template_by_id(template_id)
    occurrence_rows = []
    for occurrence in analysis.occurrences:
        for day, value in enumerate(occurrence.daily_effect, start=1):
            occurrence_rows.append(
                {
                    "day": day,
                    "effect": 100 * value,
                    "occurrence": occurrence.label,
                    "context": occurrence.context,
                }
            )
    history = pd.DataFrame(occurrence_rows)
    recommendation_rows = []
    lower_effect, upper_effect = event_uncertainty(template, template.daily_effect)
    for day, (value, lower, upper) in enumerate(
        zip(template.daily_effect, lower_effect, upper_effect), start=1
    ):
        recommendation_rows.append(
            {
                "day": day,
                "median": 100 * value,
                "q05": 100 * lower,
                "q95": 100 * upper,
            }
        )
    recommendation = pd.DataFrame(recommendation_rows)
    history_lines = alt.Chart(history).mark_line(
        color="#9ca59f", opacity=0.48, strokeWidth=1.4, point=True
    ).encode(
        x=alt.X("day:O", title="Event day"),
        y=alt.Y(
            "effect:Q",
            title="Incremental sales effect (%)",
            axis=alt.Axis(format="+.0f"),
            scale=alt.Scale(zero=True),
        ),
        detail="occurrence:N",
        tooltip=[
            alt.Tooltip("occurrence:N", title="Prior occurrence"),
            alt.Tooltip("context:N", title="Context"),
            alt.Tooltip("day:O", title="Event day"),
            alt.Tooltip("effect:Q", title="Effect", format="+.1f"),
        ],
    )
    base = alt.Chart(recommendation).encode(x=alt.X("day:O", title="Event day"))
    band = base.mark_area(color="#2f6b55", opacity=0.15).encode(
        y=alt.Y("q05:Q", scale=alt.Scale(zero=True)), y2="q95:Q"
    )
    median = base.mark_line(
        color="#2f6b55", strokeWidth=3.5, point=alt.OverlayMarkDef(filled=True, size=55)
    ).encode(
        y=alt.Y("median:Q", scale=alt.Scale(zero=True)),
        tooltip=[
            alt.Tooltip("day:O", title="Event day"),
            alt.Tooltip("median:Q", title="Recommended effect", format="+.1f"),
            alt.Tooltip("q05:Q", title="90% lower", format="+.1f"),
            alt.Tooltip("q95:Q", title="90% upper", format="+.1f"),
        ],
    )
    zero = alt.Chart(pd.DataFrame({"value": [0]})).mark_rule(
        color="#9aa39d", strokeDash=[4, 4]
    ).encode(y="value:Q")
    return (
        alt.layer(history_lines, band, zero, median)
        .properties(height=190)
        .resolve_scale(y="shared")
        .configure_view(strokeOpacity=0)
        .configure_axis(
            gridColor="#e3ded3", labelColor="#66736c", titleColor="#66736c"
        )
    )


def _event_shape_chart(template_id: str, effect: tuple[float, ...]) -> alt.Chart:
    template = template_by_id(template_id)
    rows = [{"day": 0, "median": 0.0, "q05": 0.0, "q95": 0.0}]
    lower_effect, upper_effect = event_uncertainty(template, effect)
    for day, (value, lower, upper) in enumerate(
        zip(effect, lower_effect, upper_effect), start=1
    ):
        rows.append(
            {
                "day": day,
                "median": 100 * value,
                "q05": 100 * lower,
                "q95": 100 * upper,
            }
        )
    rows.append(
        {
            "day": len(effect) + 1,
            "median": 0.0,
            "q05": 0.0,
            "q95": 0.0,
        }
    )
    frame = pd.DataFrame(rows)
    base = alt.Chart(frame).encode(
        x=alt.X(
            "day:Q",
            title="Event day",
            axis=alt.Axis(tickMinStep=1, labelExpr="datum.value == 0 ? '' : datum.value"),
        )
    )
    band = base.mark_area(color="#d96a47", opacity=0.18).encode(
        y=alt.Y(
            "q05:Q",
            title="Effect on sales (%)",
            axis=alt.Axis(format="+.0f"),
            scale=alt.Scale(zero=True),
        ),
        y2="q95:Q",
        tooltip=[
            alt.Tooltip("day:Q", title="Event day"),
            alt.Tooltip("q05:Q", title="90% lower", format="+.0f"),
            alt.Tooltip("q95:Q", title="90% upper", format="+.0f"),
        ],
    )
    median = base.mark_line(
        color="#d96a47", strokeWidth=3, point=alt.OverlayMarkDef(filled=True, size=55)
    ).encode(
        y=alt.Y("median:Q", scale=alt.Scale(zero=True)),
        tooltip=[
            alt.Tooltip("day:Q", title="Event day"),
            alt.Tooltip("median:Q", title="Median effect", format="+.0f"),
        ],
    )
    zero = alt.Chart(pd.DataFrame({"value": [0]})).mark_rule(
        color="#9aa39d", strokeDash=[4, 4]
    ).encode(y="value:Q")
    return (
        alt.layer(band, zero, median)
        .properties(height=170)
        .configure_view(strokeOpacity=0)
        .configure_axis(
            gridColor="#e3ded3", labelColor="#66736c", titleColor="#66736c"
        )
    )


def _event_catalog_matches(
    *, search: str, category: str | None, tags: list[str], suggested_only: bool
) -> list:
    query = search.strip().casefold()
    matches = []
    for template in event_catalog():
        haystack = " ".join(
            (
                template.name,
                template.category,
                template.evidence,
                template.timing_hint,
                *template.tags,
            )
        ).casefold()
        if suggested_only and not template.recommended and not query:
            continue
        if query and query not in haystack:
            continue
        if category and template.category != category:
            continue
        if tags and not all(tag in template.tags for tag in tags):
            continue
        matches.append(template)
    return matches


def _event_dates(placement: EventPlacement) -> set[date]:
    return {
        placement.start_date + timedelta(days=offset)
        for offset in range(len(placement.daily_effect))
    }


@st.dialog(
    "Plan forecast events",
    width="small",
    dismissible=False,
    position="right",
)
def _event_drawer(baseline: pd.DataFrame) -> None:
    working = _working_event_placements()
    editing_placement = _apply_pending_event_edit(working)
    horizon_start = baseline["target_date"].min().date()
    horizon_end = baseline["target_date"].max().date()
    st.markdown(
        '<span class="event-drawer-anchor"></span>',
        unsafe_allow_html=True,
    )

    if editing_placement is None:
        st.caption(
            "Choose a known event, place it in the forecast, and adjust only this "
            "scenario copy. Historical event records remain unchanged."
        )
        view_defaults = {}
        if "event_catalog_view" not in st.session_state:
            view_defaults["default"] = "Suggested"
        view = st.segmented_control(
            "Catalog view",
            ["Suggested", "Browse all"],
            key="event_catalog_view",
            width="stretch",
            **view_defaults,
        ) or "Suggested"
        search = st.text_input(
            "Search events",
            placeholder="Search names, categories, or tags",
            key="event_catalog_search",
        )
        category: str | None = None
        tags: list[str] = []
        if view == "Browse all":
            categories = sorted({template.category for template in event_catalog()})
            category = st.pills(
                "Category",
                categories,
                selection_mode="single",
                key="event_category_filter",
            )
            popular_tags = [
                "annual",
                "recurring",
                "downtown",
                "one-day",
                "two-day",
                "three-day",
                "sharp-peak",
                "slow-decay",
                "negative-tail",
            ]
            tags = st.pills(
                "Tags",
                popular_tags,
                selection_mode="multi",
                key="event_tag_filter",
            ) or []

        matches = _event_catalog_matches(
            search=search,
            category=category,
            tags=list(tags),
            suggested_only=view == "Suggested",
        )
        if not matches:
            st.info("No catalog events match those filters.")
            st.stop()
        option_ids = [template.template_id for template in matches]
        if st.session_state.get("event_template_id") not in option_ids:
            st.session_state.event_template_id = option_ids[0]
        selected_id = st.radio(
            "Event catalog",
            option_ids,
            format_func=lambda template_id: template_by_id(template_id).name,
            captions=[
                f"{template.timing_hint} · {template.evidence} · "
                f"{', '.join(template.tags[:3])}"
                for template in matches
            ],
            key="event_template_id",
            label_visibility="collapsed",
            on_change=_event_template_changed,
        )
    else:
        selected_id = editing_placement.template_id
        st.caption(
            "Editing a placed forecast occurrence. Historical event records remain unchanged."
        )
        if st.button("Choose another event", use_container_width=True):
            st.session_state.pop("event_editing_placement_id", None)
            st.session_state.event_catalog_view = "Suggested"
            st.rerun()
    template = template_by_id(selected_id)
    st.markdown(
        f'<div class="event-template-summary"><strong>{template.name}</strong>'
        f'<span>{template.category} · {template.evidence}</span></div>',
        unsafe_allow_html=True,
    )
    if editing_placement is not None:
        st.caption(
            f"Editing the placed occurrence from "
            f"{editing_placement.start_date:%b %-d}–"
            f"{editing_placement.end_date:%b %-d}. Changes below update this event only."
        )

    family_analysis = family_analysis_by_template(template.template_id)
    shape_source = "Family estimate"
    prior_occurrence_label: str | None = None
    if family_analysis is not None:
        st.markdown("#### Family-based estimate")
        st.altair_chart(
            _event_family_chart(template.template_id, family_analysis),
            width="stretch",
        )
        st.caption(
            "Gray lines are reviewed family occurrences. Dark green is the proposed "
            "next occurrence; its band is the 90% predictive range."
        )
        family_metrics = st.columns(2)
        family_metrics[0].metric("Reviewed", len(family_analysis.occurrences))
        family_metrics[1].metric("Expected peak", f"{template.peak_percent:+d}%")
        st.caption(
            f"Family direction: **{family_analysis.direction}** · "
            f"{family_analysis.evidence_quality}"
        )
        with st.expander("How the recommendation was adjusted"):
            correction_rows = [
                {
                    "Component": "Typical family peak",
                    "Peak correction": f"{family_analysis.typical_peak_percent:+.1f}%",
                }
            ]
            correction_rows.extend(
                {
                    "Component": label,
                    "Peak correction": f"{value:+.1f}%",
                }
                for label, value in family_analysis.corrections
            )
            correction_rows.append(
                {
                    "Component": "Recommended peak",
                    "Peak correction": f"{template.peak_percent:+.1f}%",
                }
            )
            st.dataframe(
                pd.DataFrame(correction_rows),
                hide_index=True,
                width="stretch",
            )
            st.caption(
                f"{family_analysis.evidence_quality}. Ordinary weekday effects remain "
                "in the baseline; these are incremental event effects."
            )

        source_key = f"event_shape_source_{template.template_id}"
        source_defaults = {}
        if source_key not in st.session_state:
            source_defaults["default"] = "Family estimate"
        shape_source = st.segmented_control(
            "Starting shape",
            ["Family estimate", "Prior occurrence", "Start blank"],
            key=source_key,
            width="stretch",
            on_change=_event_shape_source_changed,
            args=(template.template_id,),
            **source_defaults,
        ) or "Family estimate"
        if shape_source == "Prior occurrence":
            occurrence_options = [
                occurrence.label for occurrence in family_analysis.occurrences
            ]
            prior_key = f"event_prior_occurrence_{template.template_id}"
            prior_occurrence_label = st.selectbox(
                "Prior occurrence",
                occurrence_options,
                key=prior_key,
                on_change=_clear_event_day_shape,
                args=(template.template_id,),
            )
            selected_prior = next(
                occurrence
                for occurrence in family_analysis.occurrences
                if occurrence.label == prior_occurrence_label
            )
            st.caption(
                f"Starting from {selected_prior.label} · {selected_prior.context}. "
                "This remains a new forecast scenario, not a change to history."
            )
        elif shape_source == "Start blank":
            st.info(
                "The proposed occurrence starts at zero. Set its daily shape below; "
                "the family evidence remains visible for comparison."
            )
        if st.button(
            "Reset to family estimate",
            use_container_width=True,
            on_click=_reset_event_family_shape,
            args=(template.template_id,),
            disabled=shape_source == "Family estimate"
            and not bool(st.session_state.get(f"event_edit_shape_{template.template_id}")),
            key=f"reset_event_family_{template.template_id}",
        ):
            pass

    occurrence_key = f"event_occurrence_{template.template_id}"
    occurrence_defaults = {}
    if occurrence_key not in st.session_state:
        occurrence_defaults["default"] = (
            "Recurring" if "recurring" in template.tags else "One time"
        )
    occurrence = st.segmented_control(
        "Occurrence",
        ["One time", "Recurring"],
        key=occurrence_key,
        width="stretch",
        **occurrence_defaults,
    ) or "One time"
    timing_columns = st.columns(2)
    start_key = f"event_start_{template.template_id}"
    start_defaults = {}
    if start_key not in st.session_state:
        start_defaults["value"] = horizon_start
    start = timing_columns[0].date_input(
        "Start date",
        min_value=max(date.today(), horizon_start),
        max_value=horizon_end,
        key=start_key,
        **start_defaults,
    )
    maximum_duration = min(7, (horizon_end - start).days + 1)
    duration_key = f"event_duration_{template.template_id}"
    duration_defaults = {}
    if duration_key not in st.session_state:
        duration_defaults["value"] = min(template.default_duration, maximum_duration)
    elif int(st.session_state[duration_key]) > maximum_duration:
        st.session_state[duration_key] = maximum_duration
    duration = int(
        timing_columns[1].number_input(
            "Duration",
            min_value=1,
            max_value=maximum_duration,
            step=1,
            key=duration_key,
            **duration_defaults,
        )
    )
    every_weeks = 1
    if occurrence == "Recurring":
        every_weeks = int(
            st.number_input(
                "Repeat every (weeks)",
                min_value=1,
                max_value=4,
                value=1,
                step=1,
                key=f"event_repeat_{template.template_id}",
            )
        )
        starts = recurring_starts(
            start,
            horizon_end=horizon_end,
            every_weeks=every_weeks,
        )
        st.caption(
            "Planned inside this forecast: "
            + ", ".join(value.strftime("%b %-d") for value in starts)
        )

    is_closure = template.template_id == "closed-day"
    magnitude_key = f"event_magnitude_{template.template_id}"
    magnitude_defaults = {}
    if magnitude_key not in st.session_state:
        magnitude_defaults["value"] = 100
    magnitude = int(
        st.slider(
            "Overall magnitude",
            min_value=50,
            max_value=160,
            step=5,
            format="%d%%",
            disabled=is_closure,
            key=magnitude_key,
            **magnitude_defaults,
        )
    )
    starting_effect = event_source_effect(
        template.template_id,
        source=shape_source,
        duration_days=duration,
        occurrence_label=prior_occurrence_label,
    )
    suggested_effect = tuple(
        value * magnitude / 100.0 for value in starting_effect
    )
    edit_shape_key = f"event_edit_shape_{template.template_id}"
    edit_shape_defaults = {}
    if edit_shape_key not in st.session_state:
        edit_shape_defaults["value"] = False
    if shape_source == "Start blank" and not is_closure:
        edit_daily = True
        st.markdown("**Build the daily shape**")
    else:
        edit_daily = st.toggle(
            "Edit daily shape",
            disabled=is_closure,
            help="Move individual event days without changing the historical event.",
            key=edit_shape_key,
            **edit_shape_defaults,
        )
    custom_percent: tuple[float, ...] | None = None
    preview_effect = suggested_effect
    if edit_daily:
        edited: list[float] = []
        st.caption("Adjust each day. Values outside the historical range remain scenarios, not new evidence.")
        for day_index, value in enumerate(suggested_effect, start=1):
            day_key = (
                f"event_day_{template.template_id}_{duration}_{day_index}"
            )
            day_defaults = {}
            if day_key not in st.session_state:
                unscaled = starting_effect[day_index - 1]
                day_defaults["value"] = int(round(100 * unscaled))
            day_value = st.slider(
                f"Day {day_index}",
                min_value=-30,
                max_value=60,
                step=1,
                format="%d%%",
                key=day_key,
                **day_defaults,
            )
            edited.append(float(day_value))
        preview_effect = tuple(
            value * magnitude / 10_000.0 for value in edited
        )
        custom_percent = tuple(100 * value for value in preview_effect)

    st.altair_chart(
        _event_shape_chart(template.template_id, preview_effect),
        width="stretch",
    )
    st.caption(
        "Terracotta is the median event effect; the shaded area is its 90% predictive range."
    )
    peak = int(round(100 * max(preview_effect, key=abs)))
    if not is_closure and (
        magnitude > 125
        or any(
            abs(current - suggested) > 0.10
            for current, suggested in zip(preview_effect, suggested_effect)
        )
    ):
        st.warning(
            "This scenario extends beyond the strongest historical evidence, so the "
            "forecast should retain wider uncertainty."
        )
    elif is_closure:
        st.caption("Closed day sets the available sales forecast to zero.")
    else:
        st.success(f"Suggested scenario remains within prior evidence · {peak:+d}% peak")

    def apply_visible_event_edit(
        current_working: tuple[EventPlacement, ...],
    ) -> tuple[EventPlacement, ...]:
        """Fold the open event editor into plan actions before they leave the drawer."""

        if editing_placement is None:
            return current_working
        replacement = make_placement(
            template.template_id,
            start_date=start,
            duration_days=duration,
            magnitude_percent=magnitude,
            custom_daily_percent=custom_percent,
            occurrence=editing_placement.occurrence,
            placement_id=editing_placement.placement_id,
        )
        comparison_working = tuple(
            item
            for item in current_working
            if item.placement_id != editing_placement.placement_id
        )
        existing_days = (
            set().union(*(_event_dates(item) for item in comparison_working))
            if comparison_working
            else set()
        )
        if existing_days & _event_dates(replacement):
            st.session_state.event_overlap_warning = True
        updated = replace_placement(current_working, replacement)
        st.session_state.event_working_placements = updated
        clean = tuple(st.session_state.get("event_clean_reference", ()))
        st.session_state.event_dirty = not placements_equivalent(updated, clean)
        return updated

    action_label = (
        "Update selected event"
        if editing_placement is not None
        else "Add to forecast timeline"
    )
    if st.button(
        action_label,
        type="primary",
        use_container_width=True,
    ):
        starts = (
            recurring_starts(
                start,
                horizon_end=horizon_end,
                every_weeks=every_weeks,
            )
            if occurrence == "Recurring" and editing_placement is None
            else (start,)
        )
        additions = tuple(
            make_placement(
                template.template_id,
                start_date=event_start,
                duration_days=duration,
                magnitude_percent=magnitude,
                custom_daily_percent=custom_percent,
                occurrence=(
                    editing_placement.occurrence
                    if editing_placement is not None
                    else occurrence
                ),
                placement_id=(
                    editing_placement.placement_id
                    if editing_placement is not None
                    else None
                ),
            )
            for event_start in starts
        )
        comparison_working = tuple(
            item
            for item in working
            if editing_placement is None
            or item.placement_id != editing_placement.placement_id
        )
        existing_days = (
            set().union(*(_event_dates(item) for item in comparison_working))
            if comparison_working
            else set()
        )
        added_days = set().union(*(_event_dates(item) for item in additions))
        if existing_days & added_days:
            st.session_state.event_overlap_warning = True
        if editing_placement is None:
            working = working + additions
        else:
            working = replace_placement(working, additions[0])
        st.session_state.event_working_placements = working
        st.session_state.event_pending_edit_placement_id = additions[0].placement_id
        clean = tuple(st.session_state.get("event_clean_reference", ()))
        st.session_state.event_dirty = not placements_equivalent(working, clean)
        st.rerun()

    st.divider()
    st.markdown(f"**Event plan · {len(working)} placement{'s' if len(working) != 1 else ''}**")
    if st.session_state.pop("event_overlap_warning", False):
        st.warning("At least one planned event overlaps another event. Review the combined effect before implementing.")
    if not working:
        st.caption("No events are currently placed in this forecast.")
    else:
        selected_placement_id = st.session_state.get(
            "event_editing_placement_id"
        )
        for placement in working:
            effect_label = (
                "closed"
                if placement.template_id == "closed-day"
                else f"{placement.peak_percent:+d}% peak"
            )
            active = placement.placement_id == selected_placement_id
            label = (
                f"{placement.name}  ·  "
                f"{placement.start_date:%b %-d}–{placement.end_date:%b %-d}  ·  "
                f"{effect_label}"
            )
            if st.button(
                label,
                type="primary" if active else "secondary",
                use_container_width=True,
                key=f"event_plan_card_{placement.placement_id}",
            ):
                st.session_state.event_pending_edit_placement_id = (
                    placement.placement_id
                )
                st.rerun()
        if st.button(
            "Remove selected event",
            use_container_width=True,
            disabled=selected_placement_id is None,
        ):
            working = tuple(
                item for item in working if item.placement_id != selected_placement_id
            )
            st.session_state.event_working_placements = working
            st.session_state.pop("event_editing_placement_id", None)
            clean = tuple(st.session_state.get("event_clean_reference", ()))
            st.session_state.event_dirty = not placements_equivalent(working, clean)
            st.rerun()

    st.divider()
    st.caption("MVP event drafts are retained for this Streamlit session only.")
    preview_column, save_column, discard_column = st.columns(3)
    if preview_column.button("Preview", use_container_width=True):
        working = apply_visible_event_edit(working)
        st.session_state.event_draft_mode = True
        st.session_state.event_drawer_open = False
        st.rerun()
    if save_column.button("Save draft", use_container_width=True):
        working = apply_visible_event_edit(working)
        _save_event_draft_to_session()
        st.session_state.event_draft_mode = True
        st.session_state.event_drawer_open = False
        st.rerun()
    if discard_column.button("Discard", use_container_width=True):
        _clear_event_working_draft()
        st.session_state.event_draft_mode = False
        st.rerun()
    if st.button(
        "Implement event plan",
        type="primary",
        use_container_width=True,
    ):
        working = apply_visible_event_edit(working)
        st.session_state.event_published_placements = working
        st.session_state.pop("event_saved_draft", None)
        _clear_event_working_draft()
        st.session_state.event_draft_mode = False
        st.rerun()
def _render_replay(bundle) -> None:
    st.markdown('<div class="dashboard-kicker">Downtown</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="operational-title">Forecast replay</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Review exactly what an earlier forecast showed, then compare it with reported sales.</div>',
        unsafe_allow_html=True,
    )
    replay = forecast_replay_history(bundle, RESTAURANT)
    st.plotly_chart(
        forecast_replay_chart(
            replay,
            disturbance_rows(bundle, RESTAURANT),
            history_rows(bundle, RESTAURANT),
        ),
        key="downtown_replay_workspace",
        width="stretch",
        config={"displaylogo": False, "displayModeBar": False, "scrollZoom": False},
    )
    st.caption(
        "Press Play or drag the issue-date control. Events and regimes are historical model estimates shown inside each replayed horizon."
    )


def _render_forecast(bundle) -> None:
    baseline = _shifted_baseline(bundle)
    state = _plan_state()
    published_plan = _published_plan(state, baseline)
    published_marketing = score_plan(baseline, published_plan)
    published_events = _published_event_placements()
    published = score_event_plan(published_marketing.daily, published_events)
    marketing_draft_mode = bool(st.session_state.get("marketing_draft_mode"))
    event_draft_mode = bool(st.session_state.get("event_draft_mode"))
    draft_mode = marketing_draft_mode or event_draft_mode
    draft_plan = _working_plan(state, baseline) if marketing_draft_mode else None
    draft_marketing = (
        score_plan(baseline, draft_plan) if draft_plan else published_marketing
    )
    draft_events = (
        _working_event_placements() if event_draft_mode else published_events
    )
    draft = (
        score_event_plan(draft_marketing.daily, draft_events)
        if draft_mode
        else None
    )
    dirty = _has_unsaved_changes()
    _browser_leave_guard(dirty)

    title_column, mode_column = st.columns([4, 1])
    with title_column:
        st.markdown('<div class="dashboard-kicker">Downtown</div>', unsafe_allow_html=True)
        st.markdown('<h1 class="operational-title">Upcoming forecast</h1>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="section-note">{baseline["target_date"].min():%b %-d}–{baseline["target_date"].max():%b %-d} · 14-day operational view</div>',
            unsafe_allow_html=True,
        )
    with mode_column:
        if draft_mode:
            st.markdown('<div class="draft-mode-label">DRAFT COMPARISON</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="published-mode-label">IMPLEMENTED PLAN</div>', unsafe_allow_html=True)

    if draft_mode and draft is not None:
        st.markdown(
            """
            <div class="draft-banner"><strong>Draft comparison — implemented forecast unchanged</strong><br>
            <span>The terracotta line is a working plan and is not operational.</span></div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Continue editing", type="primary"):
            if event_draft_mode:
                st.session_state.event_drawer_open = True
            else:
                st.session_state.marketing_drawer_open = True
            st.rerun()

    if draft is None:
        metric_values = (
            (
                "Expected sales",
                float(published.daily["event_operational_median"].sum()),
                "90% range shown below",
            ),
            (
                "Organic baseline",
                published_marketing.summary.organic_total,
                "Before marketing and events",
            ),
            (
                "Implemented marketing lift",
                published_marketing.summary.marketing_lift,
                f"From {_money(published_marketing.summary.spend)} spend",
            ),
            (
                "Published event lift",
                published.event_lift,
                f"From {published.placement_count} planned placement{'s' if published.placement_count != 1 else ''}",
            ),
        )
    else:
        draft_total = float(draft.daily["event_operational_median"].sum())
        published_total = float(published.daily["event_operational_median"].sum())
        final_metric = (
            (
                "Total draft spend",
                draft_marketing.summary.spend,
                f"Marginal ROAS {draft_marketing.summary.marginal_roas:.2f}×",
            )
            if marketing_draft_mode
            else (
                "Draft event lift",
                draft.event_lift,
                f"From {draft.placement_count} planned placement{'s' if draft.placement_count != 1 else ''}",
            )
        )
        metric_values = (
            ("Implemented forecast", published_total, "Current operating plan"),
            ("Draft forecast", draft_total, "Not yet operational"),
            ("Draft change", draft_total - published_total, "Compared with implemented plan"),
            final_metric,
        )
    metric_columns = st.columns(4)
    for column, (label, value, help_text) in zip(metric_columns, metric_values):
        column.metric(label, _money(value, signed=label == "Draft change"), help=help_text)

    st.markdown(
        '<div class="section-title">Implemented operational forecast</div>'
        if draft is None
        else '<div class="section-title">Implemented plan compared with draft</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="section-note">Organic baseline is dashed. Green includes implemented marketing and events; terracotta is the combined draft preview.</div>',
        unsafe_allow_html=True,
    )
    latest = recent_rows(bundle, RESTAURANT)
    actual_rows = latest.loc[latest["actual_available"] & latest["actual_sales"].notna()]
    actual_value = float(actual_rows.iloc[-1]["actual_sales"]) if not actual_rows.empty else None
    st.altair_chart(
        _operational_chart(published, draft=draft, actual_value=actual_value),
        width="stretch",
    )
    serving = bundle.manifest.get("serving_snapshot", {})
    scale = serving.get("restaurant_scales", {}).get(RESTAURANT, {})
    source_origin = pd.to_datetime(
        baseline.get("source_forecast_origin", pd.Series(dtype="datetime64[ns]"))
    ).dropna()
    if scale and not source_origin.empty:
        st.caption(
            "Organic line: saved Forecast 2 posterior from "
            f"{source_origin.iloc[0]:%b %-d, %Y}, projected by forecast lead day "
            "onto this planning horizon. Dollar values use the exact inverse of "
            "the Forecast 1 preprocessing recorded with that model vintage."
        )

    marketing_column, event_column = st.columns(2, gap="large")
    with marketing_column:
        with st.container(border=True):
            st.markdown("**Marketing plan**")
            if state.published_plan is None:
                st.caption(
                    r"No marketing plan is implemented. Start at \$0 and let the optimizer propose a mix."
                )
            else:
                st.caption(
                    (
                        f"{_money(published_marketing.summary.spend)} is implemented across eligible channels; "
                        f"expected visible lift is {_money(published_marketing.summary.marketing_lift)}."
                    ).replace("$", r"\$")
                )
            if st.button("Review marketing", type="primary", key="review_marketing"):
                _begin_working_draft(state, baseline)
                st.rerun()
    with event_column:
        with st.container(border=True):
            st.markdown("**Upcoming events**")
            if published.placement_count:
                st.caption(
                    (
                        f"{published.placement_count} event placement"
                        f"{'s are' if published.placement_count != 1 else ' is'} implemented; "
                        f"expected lift is {_money(published.event_lift, signed=True)}."
                    ).replace("$", r"\$")
                )
            else:
                st.caption(
                    "No events are implemented. Start with likely events or browse the catalog."
                )
            if st.button("Review events", type="primary", key="review_events"):
                _begin_event_draft()
                st.rerun()

    with st.container(border=True):
        if (
            marketing_draft_mode
            and any(
                zone != "green" for zone in draft_marketing.saturation.values()
            )
        ):
            st.markdown("**⚠️ One decision requires review**")
            st.warning("One or more draft channel allocations enter a diminishing-return or saturated range.")
        else:
            st.markdown("**✅ No decisions require review**")
            st.caption("The forecast is within normal spending, capacity, and uncertainty ranges.")

    if st.session_state.get("marketing_drawer_open"):
        _marketing_drawer(baseline)
    if st.session_state.get("event_drawer_open"):
        _event_drawer(baseline)


def render_downtown_dashboard(bundle) -> None:
    _apply_pending_widget_clear()
    st.markdown(
        """
        <style>
        .operational-title {
            color: var(--ink); font-size: clamp(2rem, 3.4vw, 3rem); font-weight: 750;
            letter-spacing: -0.04em; line-height: 1.04; margin: 0 0 .2rem;
        }
        .draft-banner {
            background: #f4d9ce; border: 1px solid #e7b5a3; border-radius: 12px;
            padding: .8rem 1rem; margin: .25rem 0 .75rem; color: var(--ink);
        }
        .draft-banner span { color: var(--muted); font-size: .86rem; }
        .draft-mode-label, .published-mode-label {
            border-radius: 999px; padding: .45rem .7rem; text-align: center;
            font-size: .72rem; font-weight: 750; letter-spacing: .06em; margin-top: .5rem;
        }
        .draft-mode-label { background: #f4d9ce; color: #a84e34; }
        .published-mode-label { background: #dfe9e1; color: #2f6b55; }
        .saturation-labels {
            display: flex; justify-content: space-between; color: #77827b;
            font-family: "Source Sans 3", "Source Sans Pro", sans-serif;
            font-size: .6rem; line-height: 1; white-space: nowrap;
        }
        .slider-saturation-labels { margin: -.35rem 0 .3rem; }
        .channel-spend-label {
            color: var(--ink); font-size: .9rem; font-weight: 650;
            line-height: 1.15; padding-bottom: .35rem;
        }
        .channel-spend-label.disabled { color: #98a09b; }
        .channel-spend-value {
            color: var(--ink); font-size: .98rem; font-weight: 700;
            text-align: right; white-space: nowrap; padding-bottom: .35rem;
        }
        .channel-spend-value span {
            color: var(--muted); font-size: .67rem; font-weight: 500;
            margin-left: .12rem;
        }
        .manual-plan-total {
            background: #e8ebe5; border: 1px solid #d8ddd6; border-radius: 12px;
            color: #66716a; padding: .72rem .9rem; margin: .15rem 0 .5rem;
        }
        .manual-plan-total span, .manual-plan-total small {
            display: block; font-size: .72rem; line-height: 1.2;
        }
        .manual-plan-total strong {
            display: block; color: #525d56; font-size: 1.45rem;
            line-height: 1.15; margin: .15rem 0;
        }
        .manual-plan-total small { color: #879089; }
        .event-template-summary {
            display: flex; flex-direction: column; gap: .2rem;
            background: #f4e0d8; border-left: 4px solid #d96a47;
            border-radius: 10px; padding: .75rem .85rem; margin: .75rem 0;
        }
        .event-template-summary strong { color: var(--ink); font-size: 1rem; }
        .event-template-summary span { color: var(--muted); font-size: .78rem; }
        [class*="st-key-event_plan_card_"] button {
            min-height: 3.25rem; justify-content: flex-start;
            text-align: left; white-space: normal; line-height: 1.25;
            padding: .7rem .85rem;
        }
        section[role="dialog"]:has(.st-key-marketing_start_date) {
            width: min(520px, 94vw) !important;
            max-width: 520px !important;
            margin-left: auto !important;
        }
        section[role="dialog"]:has(.event-drawer-anchor) {
            width: min(500px, 96vw) !important;
            max-width: 500px !important;
            margin-left: auto !important;
        }
        section[role="dialog"]:has(.event-drawer-anchor) [data-testid="stRadio"] [role="radiogroup"] > label {
            background: #fffdf8; border: 1px solid #e1dcd1;
            border-radius: 10px; padding: .58rem .65rem; margin-bottom: .35rem;
        }
        section[role="dialog"]:has(.event-drawer-anchor) [data-testid="stVegaLiteChart"] {
            background: #f1eee7; border-radius: 12px; padding: .25rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    page = _sidebar_navigation()
    if page == PAGE_REPLAY:
        _render_replay(bundle)
    elif page == PAGE_EVENTS:
        render_events_workspace(bundle)
    else:
        _render_forecast(bundle)
