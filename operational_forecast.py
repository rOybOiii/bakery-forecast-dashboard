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
    plans_equivalent,
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


def _money(value: float, *, signed: bool = False) -> str:
    sign = "+" if signed and value > 0 else "−" if signed and value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _shifted_baseline(bundle) -> pd.DataFrame:
    """Keep the published forecast shape while presenting a current MVP horizon."""

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


def _switch_to_manual_schedule() -> None:
    st.session_state["marketing_allocation_mode"] = "Manual schedule"
    st.session_state.pop("marketing_editing_block_index", None)
    for key in list(st.session_state):
        if key.startswith("marketing_block_") or key.startswith(
            "marketing_allow_higher_"
        ):
            st.session_state.pop(key, None)


def _edit_selected_spend_block(selected_index: int) -> None:
    st.session_state["marketing_editing_block_index"] = int(selected_index)


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
        state = _plan_state()
        plan = st.session_state.marketing_working_plan
        save_plan_state(PLAN_STATE_PATH, save_draft(state, plan))
        _clear_working_draft()
        st.session_state.marketing_draft_mode = False
        _complete_navigation(target)
        st.rerun()
    if st.button("Discard changes and continue", use_container_width=True):
        _clear_working_draft()
        st.session_state.marketing_draft_mode = False
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
            [PAGE_FORECAST, PAGE_REPLAY],
            index=0 if active == PAGE_FORECAST else 1,
            label_visibility="collapsed",
            key=f"downtown_nav_picker_{nonce}",
        )
        st.button("Marketing", disabled=True, use_container_width=True)
        st.button("Events", disabled=True, use_container_width=True)
        st.caption("Marketing and event workspaces arrive after this MVP slice.")
    if requested != active:
        if bool(st.session_state.get("marketing_dirty")):
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
    published: ScenarioResult,
    *,
    draft: ScenarioResult | None,
    actual_value: float | None,
) -> alt.Chart:
    daily = published.daily.copy()
    daily["series_date"] = daily["target_date"]
    base = alt.Chart(daily).encode(
        x=alt.X("series_date:T", title=None, axis=alt.Axis(format="%b %-d", tickCount=7)),
    )
    band = base.mark_area(color="#8ba797", opacity=0.20).encode(
        y=alt.Y(
            "operational_q05:Q",
            title="Sales",
            axis=alt.Axis(format="$,.0f"),
            scale=alt.Scale(zero=False),
        ),
        y2="operational_q95:Q",
    )
    organic = base.mark_line(color="#6f8790", strokeDash=[6, 5], strokeWidth=2).encode(
        y=alt.Y("median:Q", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("series_date:T", title="Date", format="%A, %b %-d"),
            alt.Tooltip("median:Q", title="Organic baseline", format="$,.0f"),
        ],
    )
    published_line = base.mark_line(color="#2f6b55", strokeWidth=3, point=True).encode(
        y=alt.Y("operational_median:Q", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("series_date:T", title="Date", format="%A, %b %-d"),
            alt.Tooltip("operational_median:Q", title="Implemented forecast", format="$,.0f"),
            alt.Tooltip("marketing_lift:Q", title="Marketing lift", format="$,.0f"),
        ],
    )
    layers: list[alt.Chart] = [band, organic, published_line]
    if draft is not None:
        comparison = daily[["series_date", "operational_median"]].rename(
            columns={"operational_median": "published_forecast"}
        )
        comparison["draft_forecast"] = draft.daily["operational_median"].to_numpy()
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
        layers.extend([gap, draft_line])
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


def _extended_slider_amount(value: float, maximum: int) -> None:
    marker_pct = min(100.0, max(0.0, float(value) / maximum * 100.0))
    if marker_pct <= 8:
        transform = "translateX(0)"
    elif marker_pct >= 92:
        transform = "translateX(-100%)"
    else:
        transform = "translateX(-50%)"
    st.html(
        f"""
        <div class="extended-slider-amount">
          <span style="left:{marker_pct:.1f}%;transform:{transform};">
            {_money(value)}
          </span>
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
        )
        blocks = candidate.spend_blocks
    else:
        same_shape = (
            bool(plan.spend_blocks)
            and plan.start == start
            and plan.end == end
            and plan.cadence_days == cadence
        )
        blocks = (
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

        # Reflect any saved widget edits in the schedule summary before rendering it.
        # Streamlit reruns top-to-bottom, so the preview rows must read the current
        # widget state rather than the original draft blocks.
        current_blocks: list[SpendBlock] = []
        for index, block in enumerate(blocks):
            block_allows_higher = bool(
                st.session_state.get(f"marketing_allow_higher_{index}", False)
            )
            daily_spend: dict[str, float] = {}
            for key in CHANNEL_ORDER:
                slider_key = f"marketing_block_{index}_{key}"
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
                )
            )
        blocks = tuple(current_blocks)

    def block_label(index: int) -> str:
        block = blocks[index]
        if mode == "manual":
            return f"{block.start:%b %-d}–{block.end:%b %-d}"
        daily_total = sum(block.daily_spend.values())
        return (
            f"{block.start:%b %-d}–{block.end:%b %-d} · "
            f"{_money(daily_total)}/day"
        )

    block_captions = [
        " · ".join(
            f"{CHANNEL_SPECS[key].label} {_money(block.daily_spend[key])}"
            for key in CHANNEL_ORDER
        ).replace("$", r"\$")
        for block in blocks
    ]
    editing_index = (
        st.session_state.get("marketing_editing_block_index")
        if mode == "manual"
        else None
    )
    if editing_index is not None and not 0 <= int(editing_index) < len(blocks):
        st.session_state.pop("marketing_editing_block_index", None)
        editing_index = None
    st.markdown("**Spend schedule**")
    selected_block = st.radio(
        "Spending schedule",
        options=list(range(len(blocks))),
        format_func=block_label,
        captions=block_captions,
        key="marketing_selected_block_index",
        label_visibility="collapsed",
        width="stretch",
        disabled=editing_index is not None,
    )
    selected_index = int(selected_block)

    if mode == "optimized":
        selected = blocks[selected_index]
        st.caption(
            f"Selected block: {selected.duration_days} days. The optimizer staggers channel changes while preserving the total budget."
        )
        st.button(
            "Switch to manual editing",
            use_container_width=True,
            on_click=_switch_to_manual_schedule,
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
        selected = blocks[selected_index]
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
                currency_column, amount_column = value_column.columns(
                    [0.2, 0.8], gap="small", vertical_alignment="center"
                )
                currency_column.markdown(
                    '<div class="channel-currency-symbol">$</div>',
                    unsafe_allow_html=True,
                )
                direct_value = amount_column.number_input(
                    label,
                    min_value=0.0,
                    step=5.0,
                    format="%.0f",
                    disabled=not eligible[key],
                    key=extended_key,
                    label_visibility="collapsed",
                    on_change=_sync_extended_amount_to_slider,
                    args=(extended_key, extended_slider_key, normal_limits[key]),
                    **extended_defaults,
                )
                with range_column:
                    _extended_slider_amount(float(direct_value), normal_limits[key])
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
                slider_value = range_column.slider(
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
                value_column.markdown(
                    f'<div class="channel-spend-value">{_money(float(slider_value))}<span>/day</span></div>',
                    unsafe_allow_html=True,
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
    for index, block in enumerate(blocks):
        daily_spend: dict[str, float] = {}
        block_allows_higher = bool(
            st.session_state.get(f"marketing_allow_higher_{index}", False)
        )
        for key in CHANNEL_ORDER:
            if not eligible[key]:
                value = 0.0
            elif block_allows_higher and f"marketing_block_{index}_{key}_extended" in st.session_state:
                value = float(
                    st.session_state[f"marketing_block_{index}_{key}_extended"]
                )
            elif f"marketing_block_{index}_{key}" in st.session_state:
                value = float(st.session_state[f"marketing_block_{index}_{key}"])
            else:
                value = float(block.daily_spend[key])
            daily_spend[key] = value
        edited_blocks.append(
            SpendBlock(
                start_date=block.start_date,
                end_date=block.end_date,
                daily_spend=daily_spend,
                locked=index == selected_index or block.locked,
            )
        )
    total_budget = sum(
        sum(block.daily_spend.values()) * block.duration_days
        for block in edited_blocks
    )
    edited_selected = edited_blocks[selected_index]
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
    preview_column, save_column = st.columns(2)
    if preview_column.button(
        "Preview on forecast",
        type="primary",
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
    implement_column, discard_column = st.columns(2)
    if implement_column.button(
        "Implement plan", disabled=candidate is None, use_container_width=True
    ):
        save_plan_state(PLAN_STATE_PATH, implement_plan(state, candidate))
        _clear_working_draft()
        st.session_state.marketing_draft_mode = False
        st.rerun()
    if discard_column.button("Discard changes", use_container_width=True):
        _clear_working_draft()
        st.session_state.marketing_draft_mode = False
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
    published = score_plan(baseline, published_plan)
    draft_mode = bool(st.session_state.get("marketing_draft_mode"))
    draft_plan = _working_plan(state, baseline) if draft_mode else None
    draft = score_plan(baseline, draft_plan) if draft_plan else None
    dirty = bool(st.session_state.get("marketing_dirty"))
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
        banner_columns = st.columns([1, 1, 2])
        if banner_columns[0].button("Continue editing", use_container_width=True):
            st.session_state.marketing_drawer_open = True
            st.rerun()
        if banner_columns[1].button("Return to implemented", use_container_width=True):
            if dirty:
                st.session_state.marketing_exit_requested = True
                st.session_state.marketing_drawer_open = True
            else:
                _clear_working_draft()
                st.session_state.marketing_draft_mode = False
            st.rerun()

    if draft is None:
        metric_values = (
            ("Expected sales", published.summary.expected_total, "90% range shown below"),
            ("Organic baseline", published.summary.organic_total, "Before marketing"),
            ("Implemented marketing lift", published.summary.marketing_lift, f"From {_money(published.summary.spend)} spend"),
            ("Published event lift", 0.0, "Event planning is a placeholder"),
        )
    else:
        metric_values = (
            ("Implemented forecast", published.summary.expected_total, "Current operating plan"),
            ("Draft forecast", draft.summary.expected_total, "Not yet operational"),
            ("Draft change", draft.summary.expected_total - published.summary.expected_total, "Above implemented plan"),
            ("Total draft spend", draft.summary.spend, f"Marginal ROAS {draft.summary.marginal_roas:.2f}×"),
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
        '<div class="section-note">Organic baseline is dashed. Green is implemented marketing; terracotta is the draft preview.</div>',
        unsafe_allow_html=True,
    )
    latest = recent_rows(bundle, RESTAURANT)
    actual_rows = latest.loc[latest["actual_available"] & latest["actual_sales"].notna()]
    actual_value = float(actual_rows.iloc[-1]["actual_sales"]) if not actual_rows.empty else None
    st.altair_chart(
        _operational_chart(published, draft=draft, actual_value=actual_value),
        width="stretch",
    )

    marketing_column, event_column = st.columns(2, gap="large")
    with marketing_column:
        with st.container(border=True):
            st.markdown("**Marketing plan**")
            if state.published_plan is None:
                st.caption("No marketing plan is implemented. Start at $0 and let the optimizer propose a mix.")
            else:
                st.caption(
                    f"{_money(published.summary.spend)} is implemented across eligible channels; expected visible lift is {_money(published.summary.marketing_lift)}."
                )
            if st.button("Review marketing", type="primary", key="review_marketing"):
                _begin_working_draft(state, baseline)
                st.rerun()
    with event_column:
        with st.container(border=True):
            st.markdown("**Upcoming events**")
            st.caption("Event planning is reserved for the next MVP milestone.")
            st.button("Review events", disabled=True, key="review_events")

    with st.container(border=True):
        if draft is not None and any(zone != "green" for zone in draft.saturation.values()):
            st.markdown("**⚠️ One decision requires review**")
            st.warning("One or more draft channel allocations enter a diminishing-return or saturated range.")
        else:
            st.markdown("**✅ No decisions require review**")
            st.caption("The forecast is within normal spending, capacity, and uncertainty ranges.")

    if st.session_state.get("marketing_drawer_open"):
        _marketing_drawer(baseline)


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
        .channel-currency-symbol {
            color: var(--ink); font-size: 1rem; font-weight: 700;
            display: flex; align-items: center; justify-content: flex-end;
            height: 2.5rem; line-height: 1; text-align: right;
        }
        .extended-slider-amount {
            height: .95rem; position: relative; color: #2f6b55;
            font-size: .72rem; font-weight: 650; line-height: .9rem;
        }
        .extended-slider-amount span {
            position: absolute; top: 0; white-space: nowrap;
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
        section[role="dialog"]:has(.st-key-marketing_start_date) {
            width: min(520px, 94vw) !important;
            max-width: 520px !important;
            margin-left: auto !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    page = _sidebar_navigation()
    if page == PAGE_REPLAY:
        _render_replay(bundle)
    else:
        _render_forecast(bundle)
