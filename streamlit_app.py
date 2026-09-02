"""Synthetic bakery planning dashboard. No model fitting happens here."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from html import escape

import pandas as pd
import streamlit as st

from dashboard_charts import (
    forecast_chart,
    history_review_chart,
    recent_performance_chart,
)
from dashboard_data import (
    RESTAURANT_LABELS,
    disturbance_rows,
    forecast_rows,
    history_rows,
    load_bundle,
    recent_rows,
    restaurant_summary,
)
from history_window_control import history_window_control


ROOT = Path(__file__).resolve().parent
BUNDLE_DIR = ROOT / "data" / "dashboard_bundle_v1"


st.set_page_config(
    page_title="Bakery Forecast",
    page_icon="🥐",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root {
        --ink: #1d2a24;
        --green: #2f6b55;
        --cream: #f7f4ec;
        --paper: #fffdf8;
        --line: #e4dfd4;
        --muted: #6f7972;
    }
    .stApp { background: var(--cream); color: var(--ink); }
    [data-testid="stHeader"] { background: rgba(247, 244, 236, 0.92); }
    [data-testid="stSidebar"] { background: #ecf0e9; border-right: 1px solid var(--line); }
    [data-testid="stMetric"] {
        background: var(--paper);
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 3px 14px rgba(30, 42, 36, 0.04);
    }
    [data-testid="stMetricLabel"] { color: var(--muted); }
    [data-testid="stMetricValue"] { color: var(--ink); }
    .dashboard-kicker {
        color: var(--green);
        font-size: 0.78rem;
        font-weight: 750;
        letter-spacing: 0.11em;
        text-transform: uppercase;
        margin-bottom: 0.2rem;
    }
    .dashboard-title {
        color: var(--ink);
        font-size: clamp(2.15rem, 4vw, 3.5rem);
        font-weight: 750;
        letter-spacing: -0.045em;
        line-height: 1.02;
        margin: 0;
    }
    .dashboard-subtitle { color: var(--muted); margin-top: 0.55rem; }
    .section-title {
        color: var(--ink);
        font-size: 1.35rem;
        font-weight: 720;
        letter-spacing: -0.02em;
        margin: 0.4rem 0 0;
    }
    .section-note { color: var(--muted); font-size: 0.92rem; margin-bottom: 0.5rem; }
    .finding-card {
        background: var(--paper);
        border: 1px solid var(--line);
        border-left: 4px solid #d56a4a;
        border-radius: 10px;
        padding: 0.72rem 0.85rem;
        margin-bottom: 0.55rem;
    }
    .finding-card strong { color: var(--ink); }
    .finding-card small { color: var(--muted); }
    div[data-testid="stAlert"] { border-radius: 12px; }
    .block-container { max-width: 1320px; padding-top: 4.75rem; padding-bottom: 4rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def dashboard_bundle(bundle_version: int):
    """Cache a published bundle, invalidating when validation is republished."""

    del bundle_version
    return load_bundle(BUNDLE_DIR)


def money(value: float) -> str:
    return f"${value:,.0f}" if pd.notna(value) else "—"


def is_mobile_request() -> bool:
    """Use request hints to avoid touch/zoom conflicts on phones and tablets."""

    try:
        headers = st.context.headers
    except Exception:
        return False
    if str(headers.get("Sec-CH-UA-Mobile", "")).strip() == "?1":
        return True
    user_agent = str(headers.get("User-Agent", "")).lower()
    return any(
        token in user_agent
        for token in ("mobile", "android", "iphone", "ipad", "ipod")
    )


def percent(value: float) -> str:
    return f"{value:.1f}%" if pd.notna(value) else "—"


FORECAST_RANGE_FIELDS = {
    "95%": ("q025", "q975"),
    "90%": ("q05", "q95"),
    "60%": ("q20", "q80"),
    "50%": ("q25", "q75"),
    "30%": ("q35", "q65"),
}


def selected_chart_day(event, selection_name: str):
    """Extract the ISO day emitted by a Streamlit Altair point selection."""

    try:
        selected = event.selection.get(selection_name, [])
    except (AttributeError, TypeError):
        return None
    if isinstance(selected, list):
        if not selected or not isinstance(selected[0], dict):
            return None
        value = selected[0].get("selection_date")
    elif isinstance(selected, dict):
        value = selected.get("selection_date")
        if isinstance(value, list):
            value = value[0] if value else None
    else:
        return None
    return pd.Timestamp(value).date() if value else None


def mobile_forecast_details(
    frame: pd.DataFrame,
    ranges: list[str],
    *,
    selected,
) -> None:
    """Show details returned by Streamlit's chart-selection event."""

    if selected is None:
        return
    row = frame.loc[frame["target_date"].dt.date.eq(selected)].iloc[0]
    with st.container(border=True):
        st.markdown(f"**{pd.Timestamp(selected):%A, %B %-d}**")
        st.metric("Best estimate", money(row["median"]))
        for label in ("95%", "90%", "60%", "50%", "30%"):
            if label not in ranges:
                continue
            lower, upper = FORECAST_RANGE_FIELDS[label]
            st.caption(
                f"{label} plausible range: {money(row[lower])}–{money(row[upper])}"
            )


def mobile_performance_details(frame: pd.DataFrame, *, selected) -> None:
    """Show recent performance returned by a chart-selection event."""

    if selected is None:
        return
    row = frame.loc[frame["target_date"].dt.date.eq(selected)].iloc[0]
    actual_available = bool(row["actual_available"]) and pd.notna(row["actual_sales"])
    with st.container(border=True):
        st.markdown(f"**{pd.Timestamp(selected):%A, %B %-d}**")
        forecast_column, actual_column = st.columns(2)
        forecast_column.metric("Forecast", money(row["median"]))
        actual_column.metric(
            "Actual",
            money(row["actual_sales"]) if actual_available else "Not reported",
        )
        if actual_available:
            miss = abs(float(row["actual_sales"]) - float(row["median"]))
            error_rate = (
                miss / abs(float(row["actual_sales"])) * 100
                if float(row["actual_sales"]) != 0
                else float("nan")
            )
            st.caption(
                f"Absolute miss: {money(miss)} · Daily error rate: {percent(error_rate)}"
            )


bundle = dashboard_bundle((BUNDLE_DIR / "validation.json").stat().st_mtime_ns)
mobile_request = is_mobile_request()

with st.sidebar:
    st.markdown("### Planning view")
    selected_label = st.selectbox(
        "Restaurant",
        options=list(RESTAURANT_LABELS.values()),
        index=0,
    )
    restaurant = next(
        key for key, label in RESTAURANT_LABELS.items() if label == selected_label
    )
    st.caption(
        f"Forecast through {pd.Timestamp(bundle.manifest['shared_forecast_end']):%B %-d, %Y}"
    )
    st.divider()
    st.markdown("**About this preview**")
    st.caption(
        "Synthetic restaurant data is shown here. Forecasting and event analysis "
        "were completed before the dashboard was published."
    )

header, status = st.columns([4, 1])
with header:
    st.markdown('<div class="dashboard-kicker">Daily production planning</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="dashboard-title">Bakery Forecast</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="dashboard-subtitle">{selected_label} · A practical look at what is likely next</div>',
        unsafe_allow_html=True,
    )
with status:
    st.success("Data ready", icon="✅")
    st.caption(f"Updated {pd.Timestamp(bundle.manifest['generated_at']):%b %-d, %Y}")

st.info(
    "This is a synthetic demonstration. It is safe to explore and contains no client sales data.",
    icon="ℹ️",
)

summary = restaurant_summary(bundle, restaurant)
metric_columns = st.columns(4)
metric_columns[0].metric(
    "Next 14 days",
    money(summary["forecast_14_day_total"]),
    help="The sum of the best sales estimate for each of the next 14 days.",
)
metric_columns[1].metric(
    "Average forecast day",
    money(summary["forecast_average_daily_sales"]),
    help="The 14-day forecast total divided by 14 days.",
)
metric_columns[2].metric(
    "Recent average miss",
    money(summary["recent_mae_dollars"]),
    help=(
        "The average absolute dollar difference between forecast and actual sales "
        "per reported day in the recent evaluation period."
    ),
)
metric_columns[3].metric(
    "Recent error rate",
    percent(summary["recent_mape_percent"]),
    help=(
        "For each reported day, the absolute forecast error is divided by that "
        "day's actual sales; those daily percentages are then averaged. It is not "
        "the error on the 14-day sales total."
    ),
)

st.markdown('<div class="section-title">The next two weeks</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="section-note">The dark line is the best estimate. Shaded areas show other plausible outcomes.</div>',
    unsafe_allow_html=True,
)
future = forecast_rows(bundle, restaurant)
ranges = st.multiselect(
    "Show plausible ranges",
    options=["95%", "90%", "60%", "50%", "30%"],
    default=["90%", "50%", "30%"],
    help=(
        "Wider ranges include more possibilities; narrower ranges focus near the center. "
        + (
            "For All restaurants, these are approximate combined ranges formed by "
            "independently pairing draws from the four restaurant forecasts."
            if restaurant == "all"
            else ""
        )
    ),
)
if restaurant == "all":
    st.caption(
        "Combined ranges approximate the four restaurant forecasts as independent; "
        "unmeasured cross-restaurant dependence is not included."
    )
if mobile_request:
    forecast_event = st.altair_chart(
        forecast_chart(future, ranges, allow_y_navigation=False),
        key=f"mobile_forecast_chart_{restaurant}",
        width="stretch",
        on_select="rerun",
        selection_mode=["forecast_day_selection"],
    )
    st.caption(
        "The 14-day view is fixed on touch devices. Tap anywhere above a date to "
        "inspect that day's forecast."
    )
    mobile_forecast_details(
        future,
        ranges,
        selected=selected_chart_day(
            forecast_event,
            "forecast_day_selection",
        ),
    )
else:
    st.altair_chart(
        forecast_chart(future, ranges, allow_y_navigation=True),
        width="stretch",
    )
    st.caption(
        "The 14-day horizontal view is fixed. Scroll to zoom vertically; drag "
        "vertically to pan; double-click to reset."
    )

left, right = st.columns([1.75, 1], gap="large")
with left:
    st.markdown('<div class="section-title">How the last forecast performed</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note"><span style="color:#2f6b55">Forecast</span> compared with <span style="color:#d56a4a">actual sales</span>, using only information available beforehand.</div>',
        unsafe_allow_html=True,
    )
    recent = recent_rows(bundle, restaurant)
    if mobile_request:
        performance_event = st.altair_chart(
            recent_performance_chart(
                recent,
                show_range=restaurant != "all",
                allow_y_navigation=False,
            ),
            key=f"mobile_performance_chart_{restaurant}",
            width="stretch",
            on_select="rerun",
            selection_mode=["performance_day_selection"],
        )
        st.caption(
            "The 14-day view is fixed on touch devices. Tap anywhere above a date to "
            "compare forecast and actual sales."
        )
        mobile_performance_details(
            recent,
            selected=selected_chart_day(
                performance_event,
                "performance_day_selection",
            ),
        )
    else:
        st.altair_chart(
            recent_performance_chart(
                recent,
                show_range=restaurant != "all",
                allow_y_navigation=True,
            ),
            width="stretch",
        )
        st.caption(
            "The 14-day horizontal view is fixed. Scroll to zoom vertically; drag "
            "vertically to pan; double-click to reset."
        )
    observed_days = int(summary["recent_observed_days"])
    if observed_days < 14:
        st.caption(
            f"Accuracy uses {observed_days} reported days. Missing days were excluded—not treated as zero sales."
        )

with right:
    st.markdown('<div class="section-title">Recent unusual periods</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Possible events and longer sales shifts found before this forecast.</div>',
        unsafe_allow_html=True,
    )
    with st.popover(
        "What do the evidence labels mean?",
        type="tertiary",
        icon=":material/help:",
        key="evidence_guide",
    ):
        st.markdown(
            """
            **Strong evidence:** Appeared in at least 70% of plausible event histories.

            **Moderate evidence:** Appeared in 35%–69% of plausible event histories.

            **Limited evidence:** Appeared in fewer than 35% of plausible event histories.

            **Selected shift:** A longer sales shift selected for this synthetic demonstration.
            """
        )
    recent_start = recent["target_date"].min()
    recent_end = recent["target_date"].max()
    findings = disturbance_rows(bundle, restaurant)
    findings = findings.loc[
        findings["end_date"].ge(recent_start)
        & findings["start_date"].le(recent_end)
    ].head(4)
    if findings.empty:
        st.caption("No unusual periods overlapped these recent 14 days.")
    for finding in findings.itertuples(index=False):
        evidence = escape(finding.evidence_label)
        when = f"{finding.start_date:%b %-d}–{finding.end_date:%b %-d, %Y}"
        st.markdown(
            f"""
            <div class="finding-card">
              <strong>{finding.restaurant_label}: {finding.direction_label}</strong><br>
              <small>{finding.impact_label}<br>{finding.kind_label} · {finding.duration_label} · {when} · {evidence}</small>
            </div>
            """,
            unsafe_allow_html=True,
        )

with st.expander("Explore forecast, event, and sales-shift history", expanded=False):
    history = disturbance_rows(bundle, restaurant)
    historical_sales = history_rows(bundle, restaurant)
    history_min = min(
        historical_sales["target_date"].min(), history["start_lower"].min()
    ).date()
    history_max = max(
        historical_sales["target_date"].max(), history["end_upper"].max()
    ).date()
    history_months = 1 if mobile_request else 4
    initial_start = max(
        history_min,
        (pd.Timestamp(history_max) - pd.DateOffset(months=history_months)).date()
        + timedelta(days=1),
    )
    large_history = st.toggle(
        "Use a larger sales-history view",
        value=False,
        help="This enlarges only the actual-versus-expected sales chart.",
    )
    window_days = (history_max - initial_start).days
    if mobile_request:
        maximum_start = max(history_min, history_max - timedelta(days=window_days))
        selected_start = st.slider(
            "Start of the one-month history window",
            min_value=history_min,
            max_value=maximum_start,
            value=initial_start,
            step=timedelta(days=1),
            format="MMM D, YYYY",
            key=f"mobile_history_start_{restaurant}",
            help="Move this native date slider to review an earlier or later month.",
        )
        selected_end = min(
            history_max,
            selected_start + timedelta(days=window_days),
        )
        st.markdown(
            (
                '<div style="text-align:center; line-height:1.15; margin:-0.35rem 0 0.5rem;">'
                f"<strong>{selected_start:%b %-d, %Y}</strong><br>"
                "–<br>"
                f"<strong>{selected_end:%b %-d, %Y}</strong>"
                "</div>"
            ),
            unsafe_allow_html=True,
        )
    else:
        selected_start, selected_end = history_window_control(
            minimum=history_min,
            maximum=history_max,
            initial_start=initial_start,
            window_days=window_days,
            key="manager_history_window_4_month",
        )
    st.altair_chart(
        history_review_chart(
            historical_sales,
            history,
            all_restaurants=restaurant == "all",
            initial_start=pd.Timestamp(selected_start),
            initial_end=pd.Timestamp(selected_end),
            expanded=large_history,
        ),
        key="history_review_chart",
        width="stretch",
    )
    st.caption(
        "Orange is actual sales; green is the model's one-step-ahead expectation. "
        "Shaded periods mark detected events or longer shifts. These are historical "
        "model estimates, not forecasts that were previously delivered to a manager. "
        "Faint extensions show uncertain event boundaries. A sales shift is a longer period "
        "that behaved differently from the surrounding baseline. "
        + (
            "Move the one-month date slider above the charts to review earlier or later "
            "history."
            if mobile_request
            else "Drag the highlighted four-month window along the compact navigator "
            "above the charts to review earlier or later history; the charts update when "
            "you release it."
        )
    )

st.divider()
st.caption(
    "Planning aid · Synthetic demonstration · The dashboard reads completed results and never runs model sampling."
)
