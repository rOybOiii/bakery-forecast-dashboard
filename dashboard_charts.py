"""Altair charts for the manager-facing Streamlit dashboard."""

from __future__ import annotations

from datetime import timedelta

import altair as alt
import pandas as pd
import plotly.graph_objects as go


INK = "#1D2A24"
GREEN = "#2F6B55"
ADJUSTED_GREEN = "#8FB5A4"
TERRACOTTA = "#D56A4A"
GOLD = "#D9A441"
MUTED = "#768078"
HISTORY_VERTICAL_MARGIN = 0.06


def _currency_axis() -> alt.Axis:
    return alt.Axis(title=None, format="$,.0f", gridColor="#E8E4DA")


def _currency_y(field: str) -> alt.Y:
    return alt.Y(
        field,
        axis=_currency_axis(),
        scale=alt.Scale(zero=False, nice=True),
    )


def _mobile_day_selection_layers(
    frame: pd.DataFrame,
    *,
    name: str,
    color: str,
) -> list[alt.Chart]:
    """Full-height daily hit regions for Streamlit's chart-selection API."""

    hit_frame = frame[["target_date"]].drop_duplicates().copy()
    hit_frame["target_date"] = pd.to_datetime(hit_frame["target_date"])
    hit_frame["selection_date"] = hit_frame["target_date"].dt.strftime("%Y-%m-%d")
    hit_frame["hit_start"] = hit_frame["target_date"] - pd.Timedelta(hours=12)
    hit_frame["hit_end"] = hit_frame["target_date"] + pd.Timedelta(hours=12)
    selected_day = alt.selection_point(
        name=name,
        fields=["selection_date"],
        on="click",
        clear=False,
        toggle=False,
        empty=False,
    )
    hit_base = alt.Chart(hit_frame)
    selected_band = (
        hit_base.transform_filter(selected_day)
        .mark_rect(color=color, opacity=0.08)
        .encode(x=alt.X("hit_start:T", title=None), x2="hit_end:T")
    )
    selected_rule = (
        hit_base.transform_filter(selected_day)
        .mark_rule(color=color, opacity=0.65, strokeWidth=1.5)
        .encode(x=alt.X("target_date:T", title=None))
    )
    tap_target = (
        hit_base.mark_rect(opacity=0.001)
        .encode(x=alt.X("hit_start:T", title=None), x2="hit_end:T")
        .add_params(selected_day)
    )
    return [selected_band, selected_rule, tap_target]


def forecast_chart(
    frame: pd.DataFrame,
    ranges: list[str],
    allow_y_navigation: bool = True,
) -> alt.Chart:
    """Render nested forecast ranges and the median planning line."""

    base = alt.Chart(frame).encode(
        x=alt.X("target_date:T", title=None, axis=alt.Axis(format="%b %-d"))
    )
    layers: list[alt.Chart] = []
    specifications = {
        "95%": ("q025:Q", "q975:Q", GREEN, 0.07),
        "90%": ("q05:Q", "q95:Q", GREEN, 0.10),
        "60%": ("q20:Q", "q80:Q", GREEN, 0.17),
        "50%": ("q25:Q", "q75:Q", GREEN, 0.21),
        "30%": ("q35:Q", "q65:Q", GREEN, 0.27),
    }
    tap_tooltip = [
        alt.Tooltip("target_date:T", title="Date", format="%A, %b %d"),
        alt.Tooltip("median:Q", title="Best estimate", format="$,.0f"),
    ]
    for label in ("95%", "90%", "60%", "50%", "30%"):
        if label not in ranges:
            continue
        lower, upper, color, opacity = specifications[label]
        range_tooltip = [
            alt.Tooltip("target_date:T", title="Date", format="%A, %b %d"),
            alt.Tooltip(lower, title=f"{label} low", format="$,.0f"),
            alt.Tooltip(upper, title=f"{label} high", format="$,.0f"),
        ]
        tap_tooltip.extend(
            [
                alt.Tooltip(lower, title=f"{label} low", format="$,.0f"),
                alt.Tooltip(upper, title=f"{label} high", format="$,.0f"),
            ]
        )
        layers.append(
            base.mark_area(color=color, opacity=opacity).encode(
                y=_currency_y(lower),
                y2=upper,
                tooltip=range_tooltip,
            )
        )
    layers.append(
        base.mark_line(color=INK, strokeWidth=3, point=alt.OverlayMarkDef(size=42)).encode(
            y=_currency_y("median:Q")
        )
    )
    # A larger transparent point gives fingers a forgiving target without
    # changing the visible mark. Keeping the chart's domain fixed prevents a
    # slightly moving tap from being interpreted as a pan gesture on phones.
    if allow_y_navigation:
        layers.append(
            base.mark_point(filled=True, size=625, opacity=0.001).encode(
                y=_currency_y("median:Q"),
                tooltip=tap_tooltip,
            )
        )
    else:
        layers.extend(
            _mobile_day_selection_layers(
                frame,
                name="forecast_day_selection",
                color=INK,
            )
        )
    chart = (
        alt.layer(*layers)
        .properties(height=340)
    )
    if allow_y_navigation:
        chart = chart.interactive(bind_x=False, bind_y=True)
    return chart.configure_view(stroke=None).configure_axis(
        labelColor=MUTED, labelFontSize=12
    )


def recent_performance_chart(
    frame: pd.DataFrame,
    show_range: bool,
    allow_y_navigation: bool = True,
    height: int = 285,
) -> alt.Chart:
    base = alt.Chart(frame).encode(
        x=alt.X("target_date:T", title=None, axis=alt.Axis(format="%b %-d"))
    )
    layers: list[alt.Chart] = []
    if show_range:
        layers.append(
            base.mark_area(color=GREEN, opacity=0.12).encode(
                y=_currency_y("q05:Q"),
                y2="q95:Q",
                tooltip=[
                    alt.Tooltip(
                        "target_date:T",
                        title="Date",
                        format="%A, %b %d",
                    ),
                    alt.Tooltip("q05:Q", title="90% low", format="$,.0f"),
                    alt.Tooltip("q95:Q", title="90% high", format="$,.0f"),
                ],
            )
        )
    forecast = base.mark_line(
        color=GREEN,
        strokeWidth=2.5,
        point=alt.OverlayMarkDef(size=34),
    ).encode(
        y=_currency_y("median:Q")
    )
    actual = (
        base.transform_filter("datum.actual_available")
        .mark_line(color=TERRACOTTA, strokeWidth=2.5, point=alt.OverlayMarkDef(size=42))
        .encode(
            y=_currency_y("actual_sales:Q"),
        )
    )
    tap_tooltip = [
        alt.Tooltip("target_date:T", title="Date", format="%A, %b %d"),
        alt.Tooltip("median:Q", title="Forecast", format="$,.0f"),
        alt.Tooltip("actual_sales:Q", title="Actual", format="$,.0f"),
    ]
    if show_range:
        tap_tooltip.extend(
            [
                alt.Tooltip("q05:Q", title="90% low", format="$,.0f"),
                alt.Tooltip("q95:Q", title="90% high", format="$,.0f"),
            ]
        )
    forecast_tap_target = base.mark_point(
        filled=True, size=625, opacity=0.001
    ).encode(
        y=_currency_y("median:Q"),
        tooltip=tap_tooltip,
    )
    actual_tap_target = (
        base.transform_filter("datum.actual_available")
        .mark_point(filled=True, size=625, opacity=0.001)
        .encode(
            y=_currency_y("actual_sales:Q"),
            tooltip=tap_tooltip,
        )
    )
    layers.extend([forecast, actual])
    if allow_y_navigation:
        layers.extend([forecast_tap_target, actual_tap_target])
    else:
        layers.extend(
            _mobile_day_selection_layers(
                frame,
                name="performance_day_selection",
                color=TERRACOTTA,
            )
        )
    chart = (
        alt.layer(*layers)
        .properties(height=height)
    )
    if allow_y_navigation:
        chart = chart.interactive(bind_x=False, bind_y=True)
    return chart.configure_view(stroke=None).configure_axis(
        labelColor=MUTED, labelFontSize=12
    )


def forecast_replay_chart(
    frame: pd.DataFrame,
    disturbances: pd.DataFrame | None = None,
) -> go.Figure:
    """Animated, fixed-window replay of saved 14-day forecast vintages."""

    frame = frame.sort_values(["forecast_origin", "target_date"]).copy()
    origins = list(frame["forecast_origin"].drop_duplicates())
    if not origins:
        raise ValueError("Forecast replay requires at least one saved vintage")

    actual = (
        frame.loc[frame["actual_available"].astype(bool)]
        .dropna(subset=["actual_sales"])
        .sort_values(["target_date", "forecast_origin"])
        .drop_duplicates("target_date", keep="last")
    )
    disturbance_frame = (
        disturbances.copy() if disturbances is not None else pd.DataFrame()
    )
    event_counterfactual_values: list[float] = []
    if not disturbance_frame.empty:
        actual_by_date = actual.set_index("target_date")["actual_sales"]
        for row in disturbance_frame.loc[
            disturbance_frame["disturbance_type"].eq("event")
        ].itertuples(index=False):
            event_dates = pd.date_range(row.start_date, row.end_date, freq="D")
            observed = actual_by_date.reindex(event_dates).dropna()
            event_counterfactual_values.extend(
                (observed - float(row.effect_median_dollars_per_day)).tolist()
            )
    values = pd.concat(
        [
            frame["q05"],
            frame["q95"],
            actual["actual_sales"],
            pd.Series(event_counterfactual_values, dtype=float),
        ],
        ignore_index=True,
    ).dropna()
    value_low = float(values.min())
    value_high = float(values.max())
    padding = max(1.0, (value_high - value_low) * 0.08)
    y_range = [value_low - padding, value_high + padding]

    def replay_window(origin: pd.Timestamp) -> list[pd.Timestamp]:
        """Four weeks: two observed weeks, then the 14-day forecast."""

        return [
            pd.Timestamp(origin) - timedelta(days=14),
            pd.Timestamp(origin) + timedelta(days=14),
        ]

    def known_disturbance_shapes(origin: pd.Timestamp) -> list[dict[str, object]]:
        """Show only disturbance evidence plausibly available by this cutoff."""

        if disturbance_frame.empty:
            return []
        window_start, _ = replay_window(origin)
        shapes: list[dict[str, object]] = []
        for row in disturbance_frame.itertuples(index=False):
            if row.disturbance_type == "event":
                evidence_available = pd.Timestamp(row.end_upper)
                color = "rgba(213,106,74,0.05)"
            else:
                evidence_available = pd.Timestamp(row.start_upper) + timedelta(days=7)
                color = "rgba(217,164,65,0.11)"
            if evidence_available > origin:
                continue
            start = max(pd.Timestamp(row.start_date), window_start)
            end = min(pd.Timestamp(row.end_date), origin)
            if end < start:
                continue
            shapes.append(
                {
                    "type": "rect",
                    "xref": "x",
                    "yref": "paper",
                    "x0": start,
                    "x1": end + timedelta(days=1),
                    "y0": 0,
                    "y1": 1,
                    "fillcolor": color,
                    "line": {"width": 0},
                    "layer": "below",
                }
            )
        return shapes

    def replay_metric_annotations(origin: pd.Timestamp) -> list[dict[str, object]]:
        """Metrics for exactly the selected vintage's 14-day horizon."""

        vintage = frame.loc[frame["forecast_origin"].eq(origin)]
        reported = vintage.loc[
            vintage["actual_available"].astype(bool)
            & vintage["actual_sales"].notna()
        ].copy()
        reported_days = len(reported)
        if reported_days:
            average_miss = float(
                (reported["actual_sales"] - reported["median"]).abs().mean()
            )
            coverage = float(
                reported["actual_sales"].between(
                    reported["q05"], reported["q95"]
                ).mean()
            )
            miss_value = f"${average_miss:,.0f}"
            coverage_value = f"{coverage:.1%}"
        else:
            miss_value = "Waiting"
            coverage_value = "Waiting"

        labels = [
            (
                "Selected forecast",
                pd.Timestamp(origin).strftime("%b %-d, %Y"),
                "14-day horizon",
            ),
            (
                "14-day average miss",
                miss_value,
                f"{reported_days} of 14 outcomes reported",
            ),
            (
                "Inside 90% range",
                coverage_value,
                f"{reported_days} of 14 outcomes reported",
            ),
        ]
        return [
            {
                "xref": "paper",
                "yref": "paper",
                "x": x,
                "y": 1.22,
                "xanchor": "center",
                "yanchor": "middle",
                "showarrow": False,
                "align": "left",
                "width": 250,
                "bordercolor": "#DED8CB",
                "borderwidth": 1,
                "borderpad": 12,
                "bgcolor": "rgba(255,253,248,0.98)",
                "font": {"color": INK, "size": 13},
                "text": (
                    f"{title}<br><b><span style='font-size:22px'>{value}</span></b>"
                    f"<br><span style='color:{MUTED};font-size:11px'>{detail}</span>"
                ),
            }
            for x, (title, value, detail) in zip((0.14, 0.50, 0.86), labels)
        ]

    def known_event_effect_traces(origin: pd.Timestamp) -> list[go.Scatter]:
        """Show JEAM's estimated event contribution once it was knowable."""

        counterfactual_x: list[object] = []
        counterfactual_y: list[float | None] = []
        event_actual_x: list[object] = []
        event_actual_y: list[float | None] = []
        event_details: list[list[object] | None] = []
        window_start, _ = replay_window(origin)
        actual_by_date = actual.set_index("target_date")["actual_sales"]
        if not disturbance_frame.empty:
            events = disturbance_frame.loc[
                disturbance_frame["disturbance_type"].eq("event")
            ]
            for row in events.itertuples(index=False):
                if pd.Timestamp(row.end_upper) > origin:
                    continue
                start = max(pd.Timestamp(row.start_date), window_start)
                end = min(pd.Timestamp(row.end_date), origin)
                if end < start:
                    continue
                event_dates = pd.date_range(start, end, freq="D")
                observed = actual_by_date.reindex(event_dates).dropna()
                if observed.empty:
                    continue
                median_effect = float(row.effect_median_dollars_per_day)
                low_effect = float(row.effect_low_dollars_per_day)
                high_effect = float(row.effect_high_dollars_per_day)
                counterfactual = observed - median_effect
                details = [
                    [median_effect, low_effect, high_effect, row.support_probability]
                    for _ in observed
                ]
                counterfactual_x.extend([*observed.index, None])
                counterfactual_y.extend([*counterfactual.tolist(), None])
                event_actual_x.extend([*observed.index, None])
                event_actual_y.extend([*observed.tolist(), None])
                event_details.extend([*details, None])

        return [
            go.Scatter(
                x=counterfactual_x,
                y=counterfactual_y,
                mode="lines",
                line={"color": "#8B6254", "width": 2, "dash": "dash"},
                name="Estimated without event",
                connectgaps=False,
                hovertemplate=(
                    "%{x|%A, %b %d}<br>Estimated without event: $%{y:,.0f}"
                    "<extra></extra>"
                ),
            ),
            go.Scatter(
                x=event_actual_x,
                y=event_actual_y,
                mode="lines",
                line={"color": "rgba(213,106,74,0)", "width": 0},
                fill="tonexty",
                fillcolor="rgba(213,106,74,0.25)",
                name="Estimated event effect",
                connectgaps=False,
                customdata=event_details,
                hovertemplate=(
                    "%{x|%A, %b %d}<br>Estimated event effect: "
                    "$%{customdata[0]:+,.0f} per day"
                    "<br>Plausible range: $%{customdata[1]:+,.0f} to "
                    "$%{customdata[2]:+,.0f}"
                    "<br>Event support: %{customdata[3]:.0%}<extra></extra>"
                ),
            ),
        ]

    def traces(origin: pd.Timestamp) -> list[go.Scatter]:
        vintage = frame.loc[frame["forecast_origin"].eq(origin)]
        kind = str(vintage["vintage_kind"].iloc[0])
        source_label = (
            "Archived Forecast 2"
            if kind == "archived_forecast_2"
            else "Cutoff-only demo backfill"
        )
        customdata = [[source_label]] * len(vintage)
        return [
            go.Scatter(
                x=vintage["target_date"],
                y=vintage["q05"],
                mode="lines",
                line={"width": 0, "color": "rgba(47,107,85,0)"},
                hoverinfo="skip",
                showlegend=False,
            ),
            go.Scatter(
                x=vintage["target_date"],
                y=vintage["q95"],
                mode="lines",
                line={"width": 0, "color": "rgba(47,107,85,0)"},
                fill="tonexty",
                fillcolor="rgba(47,107,85,0.16)",
                name="90% forecast range",
                customdata=customdata,
                hovertemplate=(
                    "%{x|%A, %b %d}<br>90% high: $%{y:,.0f}"
                    "<br>%{customdata[0]}<extra></extra>"
                ),
            ),
            go.Scatter(
                x=vintage["target_date"],
                y=vintage["median"],
                mode="lines+markers",
                line={"color": GREEN, "width": 3},
                marker={"color": GREEN, "size": 7},
                name="Forecast",
                customdata=customdata,
                hovertemplate=(
                    "%{x|%A, %b %d}<br>Forecast: $%{y:,.0f}"
                    "<br>%{customdata[0]}<extra></extra>"
                ),
            ),
            go.Scatter(
                x=[origin, origin],
                y=y_range,
                mode="lines",
                line={"color": MUTED, "width": 1.5, "dash": "dot"},
                name="Issue date",
                hovertemplate=(
                    f"Forecast issued {pd.Timestamp(origin):%b %-d, %Y}"
                    "<extra></extra>"
                ),
            ),
        ]

    initial_origin = origins[0]
    figure = go.Figure(
        data=[
            go.Scatter(
                x=actual["target_date"],
                y=actual["actual_sales"],
                mode="lines+markers",
                line={"color": TERRACOTTA, "width": 2.5},
                marker={"color": TERRACOTTA, "size": 6},
                name="Actual sales",
                hovertemplate=(
                    "%{x|%A, %b %d}<br>Actual sales: $%{y:,.0f}<extra></extra>"
                ),
            ),
            *known_event_effect_traces(pd.Timestamp(initial_origin)),
            *traces(initial_origin),
            go.Scatter(
                x=[None],
                y=[None],
                mode="lines",
                line={"color": "rgba(213,106,74,0.35)", "width": 9},
                name="Detected event",
                hoverinfo="skip",
            ),
            go.Scatter(
                x=[None],
                y=[None],
                mode="lines",
                line={"color": "rgba(217,164,65,0.35)", "width": 9},
                name="Detected regime",
                hoverinfo="skip",
            ),
        ],
        frames=[
            go.Frame(
                name=pd.Timestamp(origin).date().isoformat(),
                data=[
                    *known_event_effect_traces(pd.Timestamp(origin)),
                    *traces(origin),
                ],
                traces=[1, 2, 3, 4, 5, 6],
                layout=go.Layout(
                    xaxis={"range": replay_window(pd.Timestamp(origin))},
                    shapes=known_disturbance_shapes(pd.Timestamp(origin)),
                    annotations=replay_metric_annotations(pd.Timestamp(origin)),
                ),
            )
            for origin in origins
        ],
    )
    slider_steps = [
        {
            "args": [
                [pd.Timestamp(origin).date().isoformat()],
                {
                    "frame": {"duration": 450, "redraw": False},
                    "mode": "immediate",
                    "transition": {"duration": 300, "easing": "cubic-in-out"},
                },
            ],
            "label": (
                pd.Timestamp(origin).strftime("%b %-d")
                if (
                    (pd.Timestamp(origin) - pd.Timestamp(origins[0])).days % 7 == 0
                    or origin == origins[-1]
                )
                else ""
            ),
            "method": "animate",
        }
        for origin in origins
    ]
    figure.update_layout(
        height=620,
        margin={"l": 15, "r": 20, "t": 130, "b": 20},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": INK},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.04, "x": 0},
        shapes=known_disturbance_shapes(pd.Timestamp(initial_origin)),
        annotations=replay_metric_annotations(pd.Timestamp(initial_origin)),
        xaxis={
            "range": replay_window(pd.Timestamp(initial_origin)),
            "tickformat": "%b %-d",
            "showgrid": False,
            "title": None,
        },
        yaxis={
            "range": y_range,
            "tickprefix": "$",
            "tickformat": ",.0f",
            "gridcolor": "#E8E4DA",
            "title": None,
        },
        updatemenus=[
            {
                "type": "buttons",
                "direction": "left",
                "x": 0,
                "y": -0.16,
                "showactive": False,
                "buttons": [
                    {
                        "label": "▶ Play",
                        "method": "animate",
                        "args": [
                            None,
                            {
                                "frame": {"duration": 320, "redraw": False},
                                "fromcurrent": True,
                                "transition": {
                                    "duration": 240,
                                    "easing": "cubic-in-out",
                                },
                            },
                        ],
                    },
                    {
                        "label": "Pause",
                        "method": "animate",
                        "args": [
                            [None],
                            {
                                "frame": {"duration": 0, "redraw": False},
                                "mode": "immediate",
                                "transition": {"duration": 0},
                            },
                        ],
                    },
                ],
            }
        ],
        sliders=[
            {
                "active": 0,
                "currentvalue": {"prefix": "Forecast issued: ", "font": {"size": 14}},
                "pad": {"t": 48, "l": 145},
                "steps": slider_steps,
            }
        ],
    )
    return figure


def disturbance_timeline(
    frame: pd.DataFrame, all_restaurants: bool, expanded: bool = False
) -> alt.Chart:
    y_field = "restaurant_label:N" if all_restaurants else "kind_label:N"
    y_title = None
    base = alt.Chart(frame).encode(
        x=alt.X("start_lower:T", title=None, axis=alt.Axis(format="%b %Y")),
        x2="end_upper:T",
        y=alt.Y(y_field, title=y_title),
        color=alt.Color(
            "disturbance_type:N",
            title=None,
            scale=alt.Scale(
                domain=["event", "regime"], range=[TERRACOTTA, GOLD]
            ),
            legend=alt.Legend(
                labelExpr="datum.label == 'event' ? 'Event' : 'Long-lasting shift'",
                orient="top",
            ),
        ),
    )
    uncertainty = base.mark_bar(opacity=0.16, size=18)
    core = alt.Chart(frame).mark_bar(size=8, cornerRadius=4).encode(
        x=alt.X("start_date:T", title=None, axis=alt.Axis(format="%b %Y")),
        x2="end_date:T",
        y=alt.Y(y_field, title=y_title),
        color=alt.Color(
            "disturbance_type:N",
            scale=alt.Scale(
                domain=["event", "regime"], range=[TERRACOTTA, GOLD]
            ),
            legend=None,
        ),
        tooltip=[
            alt.Tooltip("restaurant_label:N", title="Restaurant"),
            alt.Tooltip("kind_label:N", title="What we found"),
            alt.Tooltip("impact_label:N", title="Size and direction"),
            alt.Tooltip("duration_label:N", title="Duration"),
            alt.Tooltip("evidence_label:N", title="Evidence"),
            alt.Tooltip("start_date:T", title="Started", format="%b %d, %Y"),
            alt.Tooltip("end_date:T", title="Ended", format="%b %d, %Y"),
        ],
    )
    return (
        alt.layer(uncertainty, core)
        .properties(
            height=(480 if all_restaurants else 360)
            if expanded
            else (240 if all_restaurants else 180)
        )
        .configure_view(stroke=None)
        .configure_axis(labelColor=MUTED, labelFontSize=12, grid=False)
    )


def _history_hover_frame(sales: pd.DataFrame) -> pd.DataFrame:
    """Add manager-friendly, nearest-day tooltip content to history rows."""

    frame = sales.copy()
    frame["baseline_hover"] = frame["baseline_median"].map(
        lambda value: f"${value:,.0f}" if pd.notna(value) else "Not available"
    )
    frame["adjusted_hover"] = frame["median"].map(
        lambda value: f"${value:,.0f}" if pd.notna(value) else "Not available"
    )
    frame["actual_hover"] = frame["actual_sales"].map(
        lambda value: f"${value:,.0f}" if pd.notna(value) else "Not reported"
    )

    return frame


def _history_y_domain(sales: pd.DataFrame) -> list[float]:
    """Keep a stable, lightly padded sales scale across history windows."""

    actual_mask = sales["actual_available"].astype(bool)
    values = pd.concat(
        [
            sales["median"],
            sales["baseline_median"],
            sales.loc[actual_mask, "actual_sales"],
        ],
        ignore_index=True,
    ).dropna()
    value_low = float(values.min())
    value_high = float(values.max())
    if value_high <= value_low:
        value_high = value_low + 1.0
    value_span = value_high - value_low
    return [
        value_low - value_span * HISTORY_VERTICAL_MARGIN,
        value_high + value_span * HISTORY_VERTICAL_MARGIN,
    ]


def history_review_chart(
    sales: pd.DataFrame,
    disturbances: pd.DataFrame,
    *,
    all_restaurants: bool,
    initial_start: pd.Timestamp,
    initial_end: pd.Timestamp,
    expanded: bool = False,
) -> alt.Chart:
    """History detail for a fixed window selected by the external navigator."""

    window_scale = alt.Scale(
        domain=[initial_start.isoformat(), initial_end.isoformat()],
        nice=False,
    )
    span_days = max(1, (initial_end - initial_start).days)
    tick_step_days = 7 if span_days <= 45 else 14
    history_tick_values = [
        value.to_pydatetime()
        for value in pd.date_range(
            initial_start.normalize(),
            initial_end.normalize(),
            freq=f"{tick_step_days}D",
        )
    ]
    axis_options = {
        "format": "%b %-d",
        "values": history_tick_values,
        # Keep edge labels inside the scale range. Otherwise fit-x changes
        # the plot width as differently sized first/last dates appear.
        "labelBound": True,
        "labelFlush": True,
    }
    visible_sales = sales.loc[
        sales["target_date"].between(initial_start, initial_end)
    ]
    visible_disturbances = disturbances.loc[
        disturbances["start_lower"].le(initial_end)
        & disturbances["end_upper"].ge(initial_start)
    ]
    detail_x = alt.X(
        "target_date:T",
        title=None,
        scale=window_scale,
        axis=alt.Axis(**axis_options),
    )
    layers: list[alt.Chart] = []
    if not visible_disturbances.empty:
        periods = alt.Chart(visible_disturbances).mark_rect(opacity=0.08, clip=True).encode(
            x=alt.X("start_lower:T", scale=window_scale),
            x2="end_upper:T",
            color=alt.Color(
                "disturbance_type:N",
                title=None,
                scale=alt.Scale(
                    domain=["event", "regime"], range=[TERRACOTTA, GOLD]
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("restaurant_label:N", title="Restaurant"),
                alt.Tooltip("kind_label:N", title="Unusual period"),
                alt.Tooltip("impact_label:N", title="Size and direction"),
                alt.Tooltip("duration_label:N", title="Duration"),
                alt.Tooltip("evidence_label:N", title="Evidence"),
            ],
        )
        layers.append(periods)

    hover_rows = _history_hover_frame(visible_sales)
    history_y_domain = _history_y_domain(sales)
    history_y_scale = alt.Scale(
        domain=history_y_domain,
        zero=False,
        nice=True,
    )
    adjusted_line = (
        alt.Chart(visible_sales)
        .mark_line(
            color=ADJUSTED_GREEN,
            strokeWidth=2.5,
            strokeCap="round",
            strokeJoin="round",
            clip=True,
        )
        .encode(
            x=detail_x,
            y=alt.Y(
                "median:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            ),
        )
    )
    baseline_line = (
        alt.Chart(visible_sales)
        .mark_line(
            color=GREEN,
            strokeWidth=3,
            strokeCap="round",
            strokeJoin="round",
            clip=True,
        )
        .encode(
            x=detail_x,
            y=alt.Y(
                "baseline_median:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            ),
        )
    )
    # Missing reports are omitted from the path so the neighboring observed
    # sales days remain connected, matching the original history display.
    actual_line_source = visible_sales.loc[
        visible_sales["actual_available"].astype(bool)
        & visible_sales["actual_sales"].notna()
    ]
    actual_line = (
        alt.Chart(actual_line_source)
        .mark_line(
            color=TERRACOTTA,
            opacity=0.86,
            strokeWidth=2,
            strokeCap="round",
            strokeJoin="round",
            clip=True,
        )
        .encode(
            x=detail_x,
            y=alt.Y(
                "actual_sales:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            ),
        )
    )
    nearest_day = alt.selection_point(
        name="history_nearest_day",
        fields=["target_date"],
        nearest=True,
        on="pointerover",
        clear="pointerout",
        empty=False,
    )
    hover_base = alt.Chart(hover_rows).encode(x=detail_x)
    hover_rule = (
        hover_base.transform_filter(nearest_day)
        .mark_rule(color=MUTED, opacity=0.42, strokeWidth=1)
    )
    baseline_hover_point = (
        hover_base.transform_filter(nearest_day)
        .mark_point(color=GREEN, filled=True, size=58)
        .encode(
            y=alt.Y(
                "baseline_median:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            )
        )
    )
    adjusted_hover_point = (
        hover_base.transform_filter(nearest_day)
        .mark_point(color=ADJUSTED_GREEN, filled=True, size=52)
        .encode(
            y=alt.Y(
                "median:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            )
        )
    )
    actual_hover_point = (
        hover_base.transform_filter(nearest_day)
        .transform_filter("datum.actual_available")
        .mark_point(color=TERRACOTTA, filled=True, size=58)
        .encode(
            y=alt.Y(
                "actual_sales:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            )
        )
    )
    hover_targets = (
        hover_base.mark_point(opacity=0)
        .encode(
            y=alt.Y(
                "baseline_median:Q",
                axis=_currency_axis(),
                scale=history_y_scale,
            ),
            tooltip=[
                alt.Tooltip("target_date:T", title="Date", format="%A, %b %d, %Y"),
                alt.Tooltip(
                    "baseline_hover:N",
                    title="Forecast without unusual periods",
                ),
                alt.Tooltip(
                    "adjusted_hover:N",
                    title="Forecast after explaining unusual periods",
                ),
                alt.Tooltip("actual_hover:N", title="Actual sales"),
            ],
        )
        .add_params(nearest_day)
    )
    layers.extend(
        [
            adjusted_line,
            baseline_line,
            actual_line,
            hover_rule,
            baseline_hover_point,
            adjusted_hover_point,
            actual_hover_point,
            hover_targets,
        ]
    )
    sales_detail = (
        alt.layer(*layers)
        .properties(width="container", height=390 if expanded else 235)
        .interactive(name="history_y_zoom", bind_x=False, bind_y=True)
    )

    y_field = "restaurant_label:N" if all_restaurants else "kind_label:N"
    timeline_base = alt.Chart(visible_disturbances).encode(
        x=alt.X(
            "start_lower:T",
            title=None,
            scale=window_scale,
            axis=alt.Axis(**axis_options),
        ),
        x2="end_upper:T",
        y=alt.Y(y_field, title=None),
        color=alt.Color(
            "disturbance_type:N",
            title=None,
            scale=alt.Scale(
                domain=["event", "regime"], range=[TERRACOTTA, GOLD]
            ),
            legend=alt.Legend(
                labelExpr="datum.label == 'event' ? 'Event' : 'Long-lasting shift'",
                orient="top",
            ),
        ),
    )
    uncertainty = timeline_base.mark_bar(opacity=0.16, size=18, clip=True)
    core = alt.Chart(visible_disturbances).mark_bar(
        size=8, cornerRadius=4, clip=True
    ).encode(
        x=alt.X(
            "start_date:T",
            scale=window_scale,
            title=None,
            axis=alt.Axis(**axis_options),
        ),
        x2="end_date:T",
        y=alt.Y(y_field, title=None),
        color=alt.Color(
            "disturbance_type:N",
            scale=alt.Scale(
                domain=["event", "regime"], range=[TERRACOTTA, GOLD]
            ),
            legend=None,
        ),
        tooltip=[
            alt.Tooltip("restaurant_label:N", title="Restaurant"),
            alt.Tooltip("kind_label:N", title="What we found"),
            alt.Tooltip("impact_label:N", title="Size and direction"),
            alt.Tooltip("duration_label:N", title="Duration"),
            alt.Tooltip("evidence_label:N", title="Evidence"),
            alt.Tooltip("start_date:T", title="Started", format="%b %d, %Y"),
            alt.Tooltip("end_date:T", title="Ended", format="%b %d, %Y"),
        ],
    )
    timeline_detail = alt.layer(uncertainty, core).properties(
        width="container",
        height=210 if all_restaurants else 145,
    )

    return (
        alt.vconcat(
            sales_detail,
            timeline_detail,
            # Edge labels are explicitly bounded above, so full bounds can
            # safely reserve vertical room for axes, legends, and row labels
            # without changing the plot width as the date window moves.
            bounds="full",
            center=False,
            spacing=10,
        )
        .properties(
            autosize=alt.AutoSizeParams(
                type="fit-x",
                contains="padding",
                resize=True,
            ),
            # Keep timeline-axis descenders (for example, the "p" in Sep)
            # inside the chart canvas instead of letting the caption below
            # visually clip them.
            padding={"left": 0, "right": 0, "top": 0, "bottom": 12},
            usermeta={"embedOptions": {"renderer": "svg"}},
        )
        .resolve_scale(x="shared", y="independent")
        .configure_view(stroke=None)
        .configure_axis(labelColor=MUTED, labelFontSize=12, grid=False)
    )
