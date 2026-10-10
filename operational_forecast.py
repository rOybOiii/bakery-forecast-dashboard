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
    )
    for key in widget_keys:
        st.session_state.pop(key, None)
    for channel in CHANNEL_ORDER:
        st.session_state.pop(f"marketing_eligible_{channel}", None)
        st.session_state.pop(f"marketing_manual_{channel}", None)


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


def _saturation_bar(channel: str, amount: float, duration_days: int) -> None:
    yellow, red = saturation_thresholds(channel, duration_days)
    maximum = max(red * 1.35, amount * 1.10, 1.0)
    marker = min(100.0, amount / maximum * 100.0)
    yellow_pct = yellow / maximum * 100.0
    red_pct = red / maximum * 100.0
    st.markdown(
        f"""
        <div class="saturation-row">
          <div class="saturation-track" style="background:linear-gradient(90deg,
            #8eb59e 0%, #8eb59e {yellow_pct:.1f}%,
            #e2bc62 {yellow_pct:.1f}%, #e2bc62 {red_pct:.1f}%,
            #d86d53 {red_pct:.1f}%, #d86d53 100%);">
            <span class="saturation-marker" style="left:{marker:.1f}%"></span>
          </div>
          <div class="saturation-labels"><span>efficient</span><span>diminishing</span><span>saturated</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _candidate_from_controls(plan: MarketingPlan) -> MarketingPlan | None:
    period_default = (
        "7 days" if plan.duration_days == 7 else "14 days" if plan.duration_days == 14 else "Custom"
    )
    start = st.date_input(
        "Plan starts",
        value=max(plan.start, date.today()),
        min_value=date.today(),
        key="marketing_start_date",
    )
    period = st.segmented_control(
        "Plan period",
        options=["7 days", "14 days", "Custom"],
        default=period_default,
        key="marketing_period",
    ) or period_default
    if period == "7 days":
        end = start + timedelta(days=6)
    elif period == "14 days":
        end = start + timedelta(days=13)
    else:
        end = st.date_input(
            "Plan ends",
            value=max(plan.end, start),
            min_value=start,
            key="marketing_end_date",
        )
    duration = (end - start).days + 1
    total_budget = st.slider(
        "Total planned spend",
        min_value=0,
        max_value=15_000,
        value=int(round(plan.total_budget / 100.0) * 100),
        step=100,
        format="$%d",
        key="marketing_total_budget",
    )
    st.caption(
        f"{start:%b %-d}–{end:%b %-d} · spending changes forecast values, not the 14-day horizon"
    )
    st.markdown("**Eligible channels**")
    eligibility_columns = st.columns(3)
    eligible: dict[str, bool] = {}
    for column, key in zip(eligibility_columns, CHANNEL_ORDER):
        eligible[key] = column.toggle(
            CHANNEL_SPECS[key].label,
            value=plan.channels[key].eligible,
            key=f"marketing_eligible_{key}",
        )
    if total_budget > 0 and not any(eligible.values()):
        st.error("Turn on at least one channel before allocating a positive budget.")
        return None
    mode_label = st.radio(
        "Allocation",
        ["Optimize mix", "Manual override"],
        index=0 if plan.allocation_mode == "optimized" else 1,
        horizontal=True,
        key="marketing_allocation_mode",
    )
    mode = "optimized" if mode_label == "Optimize mix" else "manual"
    if mode == "optimized":
        allocations = None
        preview = make_plan(
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
        )
        values = {key: preview.channels[key].amount for key in CHANNEL_ORDER}
    else:
        values = {}
        remaining = float(total_budget)
        eligible_keys = [key for key in CHANNEL_ORDER if eligible[key]]
        for index, key in enumerate(CHANNEL_ORDER):
            default = plan.channels[key].amount if eligible[key] else 0.0
            if eligible[key] and index == max(
                (CHANNEL_ORDER.index(value) for value in eligible_keys), default=-1
            ):
                default = max(0.0, remaining)
            amount = st.number_input(
                f"{CHANNEL_SPECS[key].label} allocation",
                min_value=0.0,
                max_value=float(total_budget),
                value=float(min(default, total_budget)) if eligible[key] else 0.0,
                step=100.0,
                disabled=not eligible[key],
                key=f"marketing_manual_{key}",
            )
            values[key] = float(amount) if eligible[key] else 0.0
            remaining -= values[key]
        if abs(sum(values.values()) - total_budget) > 0.02:
            st.warning(
                f"Channel allocations must total {_money(total_budget)}; currently {_money(sum(values.values()))}."
            )
            return None
        allocations = values
    st.markdown("**Proposed channel mix**")
    for key in CHANNEL_ORDER:
        label = CHANNEL_SPECS[key].label
        amount = values[key]
        label_column, value_column = st.columns([3, 1])
        label_column.caption(label if eligible[key] else f"{label} · off")
        value_column.markdown(f"<div style='text-align:right'>{_money(amount)}</div>", unsafe_allow_html=True)
        _saturation_bar(key, amount, duration)
    return make_plan(
        restaurant=RESTAURANT,
        start_date=start,
        end_date=end,
        total_budget=float(total_budget),
        eligible=eligible,
        allocation_mode=mode,
        allocations=allocations,
        baseline_vintage=BASELINE_VINTAGE,
        mmm_fit_version=MMM_FIT_VERSION,
        plan_id=plan.plan_id,
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
        .saturation-row { margin: -.25rem 0 .55rem; }
        .saturation-track { position: relative; height: 8px; border-radius: 999px; overflow: visible; }
        .saturation-marker {
            position: absolute; width: 3px; height: 15px; top: -3px; border-radius: 2px;
            background: #1d2a24; transform: translateX(-1px);
        }
        .saturation-labels { display: flex; justify-content: space-between; color: #77827b; font-size: .65rem; margin-top: 2px; }
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
