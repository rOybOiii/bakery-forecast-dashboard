"""Database-backed MMM setup and spend-learning workspace."""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import text
from gateway_client import (
    GatewayOnboardingRepository,
    GatewayPipelineRepository,
    GatewayWorkflowController,
    active_gateway_scope,
    application_call,
)


ROOT = Path(__file__).resolve().parent


def _pipeline_repository():
    if active_gateway_scope() is not None:
        return GatewayPipelineRepository()
    try:
        from credent_orchestrator import PipelineRepository
    except ImportError:
        source = ROOT.parent / "Credent Core" / "packages" / "orchestrator" / "src"
        if source.exists() and str(source) not in sys.path:
            sys.path.insert(0, str(source))
        from credent_orchestrator import PipelineRepository
    return PipelineRepository()


def _engine():
    try:
        from credent_db import make_engine
    except ImportError:
        return None
    return make_engine()


def _current_operational_summary(
    *, site_id: int, metric_id: int
) -> dict[str, Any] | None:
    """Read the published Rescore result used by the Forecast page."""

    if active_gateway_scope() is not None:
        return application_call(
            "workspace", "marketing.current_summary", site_id=site_id, metric_id=metric_id
        )

    engine = _engine()
    if engine is None:
        return None
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT score.id, score.scoring_run_id,
                        score.source_forecast_run_id, score.qualification_id,
                        score.plan_id, score.health_status, score.published_at,
                        plan.budget AS planned_spend,
                        COALESCE(SUM(point.base_median), 0) AS organic_total,
                        COALESCE(SUM(point.carryover_median), 0) AS carryover,
                        COALESCE(SUM(point.planned_lift_median), 0) AS planned_lift,
                        COALESCE(SUM(point.marketing_lift_median), 0) AS marketing_lift,
                        COALESCE(SUM(point.combined_median), 0) AS scored_total
                    FROM mmm_program program
                    JOIN mmm_operational_score score
                      ON score.program_id = program.id
                     AND score.state = 'current'
                    JOIN mmm_operational_score_point point
                      ON point.score_id = score.id
                    LEFT JOIN spend_plan plan ON plan.id = score.plan_id
                    WHERE program.site_id = :site_id
                      AND program.metric_id = :metric_id
                    GROUP BY score.id, plan.id
                    ORDER BY score.published_at DESC, score.id DESC
                    LIMIT 1
                    """
                ),
                {"site_id": site_id, "metric_id": metric_id},
            ).mappings().one_or_none()
    finally:
        engine.dispose()
    return dict(row) if row is not None else None


def _qualification_history(*, site_id: int, metric_id: int) -> list[dict[str, Any]]:
    """Return the durable eligibility log, including administrative decisions."""

    if active_gateway_scope() is not None:
        return application_call(
            "workspace", "marketing.qualification_history", site_id=site_id, metric_id=metric_id
        )

    engine = _engine()
    if engine is None:
        return []
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT qualification.id, qualification.learning_run_id,
                        qualification.decision, qualification.state,
                        qualification.summary, qualification.decided_at,
                        override.id AS override_id,
                        override.status AS override_status,
                        override.reason AS override_reason,
                        account.name AS override_approved_by
                    FROM mmm_program program
                    JOIN mmm_qualification qualification
                      ON qualification.program_id = program.id
                    LEFT JOIN mmm_operational_override override
                      ON override.qualification_id = qualification.id
                    LEFT JOIN account ON account.id = override.approved_by
                    WHERE program.site_id = :site_id
                      AND program.metric_id = :metric_id
                    ORDER BY qualification.decided_at DESC,
                        qualification.id DESC
                    LIMIT 25
                    """
                ),
                {"site_id": site_id, "metric_id": metric_id},
            ).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def _plan_schedule(plan_id: int) -> pd.DataFrame:
    """Return the immutable implemented block schedule for manager review."""

    if active_gateway_scope() is not None:
        return application_call("workspace", "marketing.plan_schedule", plan_id)

    engine = _engine()
    if engine is None:
        return pd.DataFrame()
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT block.sequence, block.start_date, block.end_date,
                        block.pause, block.locked, channel.name,
                        spend.daily_spend
                    FROM spend_block block
                    JOIN channel_spend spend ON spend.block_id = block.id
                    JOIN channel ON channel.id = spend.channel_id
                    WHERE block.plan_id = :plan_id
                    ORDER BY block.sequence, channel.name
                    """
                ),
                {"plan_id": plan_id},
            ).mappings().all()
    finally:
        engine.dispose()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame([dict(row) for row in rows])
    schedule = frame.pivot_table(
        index=["sequence", "start_date", "end_date", "pause", "locked"],
        columns="name",
        values="daily_spend",
        aggfunc="first",
    ).reset_index()
    schedule.columns.name = None
    return schedule.rename(
        columns={
            "sequence": "Block",
            "start_date": "Start",
            "end_date": "End",
            "pause": "Pause",
            "locked": "Locked",
        }
    )


def _plan_daily_detail(plan_id: int, metric_id: int) -> pd.DataFrame:
    """Return planned, confirmed, and sales-ready values for every plan day."""

    if active_gateway_scope() is not None:
        return application_call(
            "workspace", "marketing.plan_daily_detail", plan_id, metric_id
        )

    engine = _engine()
    if engine is None:
        return pd.DataFrame()
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT block.sequence, day::date AS date, block.pause,
                        spend.channel_id, channel.name AS channel,
                        spend.daily_spend AS planned_spend,
                        actual.amount AS actual_spend,
                        actual.revision AS actual_revision,
                        observed.value IS NOT NULL AS sales_available
                    FROM spend_block block
                    JOIN channel_spend spend ON spend.block_id = block.id
                    JOIN channel ON channel.id = spend.channel_id
                    CROSS JOIN LATERAL generate_series(
                        block.start_date, block.end_date, INTERVAL '1 day'
                    ) day
                    JOIN spend_plan plan ON plan.id = block.plan_id
                    LEFT JOIN actual_spend actual
                      ON actual.source_plan_id = plan.id
                     AND actual.channel_id = spend.channel_id
                     AND actual.date = day::date
                     AND actual.state = 'current'
                    LEFT JOIN current_observation observed
                      ON observed.site_id = plan.site_id
                     AND observed.metric_id = :metric_id
                     AND observed.date = day::date
                    WHERE plan.id = :plan_id
                    ORDER BY day, block.sequence, channel.name
                    """
                ),
                {"plan_id": plan_id, "metric_id": metric_id},
            ).mappings().all()
    finally:
        engine.dispose()
    return pd.DataFrame([dict(row) for row in rows])


def _marketing_observations(*, site_id: int, metric_id: int) -> pd.DataFrame:
    if active_gateway_scope() is not None:
        return application_call(
            "workspace", "marketing.observations", site_id=site_id, metric_id=metric_id
        )
    engine = _engine()
    if engine is None:
        return pd.DataFrame(columns=["date", "value"])
    try:
        with engine.connect() as connection:
            return pd.read_sql(
                text(
                    """
                    SELECT date, value FROM current_observation
                    WHERE site_id = :site_id AND metric_id = :metric_id
                    ORDER BY date
                    """
                ),
                connection,
                params={"site_id": site_id, "metric_id": metric_id},
            )
    finally:
        engine.dispose()


def _onboarding_repository():
    if active_gateway_scope() is not None:
        return GatewayOnboardingRepository()
    try:
        from credent_onboarding import OnboardingRepository
    except ImportError:
        return None
    return OnboardingRepository(artifact_dir=ROOT.parent / ".credent-artifacts")


def _workflow_controller(pipeline):
    if isinstance(pipeline, GatewayPipelineRepository):
        return GatewayWorkflowController()
    try:
        from credent_orchestrator import WorkflowController
    except ImportError:
        return None
    return WorkflowController(pipeline)


def normalize_historical_spend(
    table: pd.DataFrame,
    channel_names: list[str],
) -> list[dict[str, Any]]:
    """Turn a Date + channel-column table into confirmed daily spend rows."""

    if not isinstance(table, pd.DataFrame) or table.empty:
        raise ValueError("the historical spend file is empty")
    lookup = {str(column).strip().casefold(): column for column in table.columns}
    date_column = lookup.get("date") or lookup.get("day")
    if date_column is None:
        raise ValueError("historical spend needs a Date column")
    configured: list[tuple[str, object]] = []
    for name in channel_names:
        column = lookup.get(name.strip().casefold())
        if column is None:
            raise ValueError(f"historical spend is missing the {name} column")
        configured.append((name, column))
    dates = pd.to_datetime(table[date_column], errors="coerce")
    if dates.isna().any():
        raise ValueError("every historical spend row needs a valid date")
    if dates.dt.date.duplicated().any():
        raise ValueError("historical spend contains duplicate dates")
    if (dates.dt.date > date.today()).any():
        raise ValueError("historical spend cannot include future dates")
    rows: list[dict[str, Any]] = []
    for name, column in configured:
        amounts = pd.to_numeric(table[column], errors="coerce")
        if amounts.isna().any():
            raise ValueError(f"every {name} value must be numeric")
        if (amounts < 0).any():
            raise ValueError(f"{name} spend cannot be negative")
        rows.extend(
            {
                "date": spend_date.isoformat(),
                "channel": name,
                "amount": float(amount),
            }
            for spend_date, amount in zip(dates.dt.date, amounts, strict=True)
        )
    return rows


def _read_spend_upload(upload) -> pd.DataFrame:
    payload = upload.getvalue()
    suffix = Path(upload.name).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(BytesIO(payload))
    return pd.read_csv(BytesIO(payload))


def _block_layout(
    *,
    start_date: date,
    cadence_days: int,
    pause_after: dict[int, int],
    duration_days: int = 14,
) -> list[tuple[date, date, bool, int]]:
    """Build contiguous active and zero-spend blocks across one plan period."""

    base_durations: list[int] = []
    remaining = duration_days
    while remaining:
        size = min(cadence_days, remaining)
        base_durations.append(size)
        remaining -= size
    maximum_pause = max(0, duration_days - len(base_durations))
    pause_budget = min(sum(max(0, int(value)) for value in pause_after.values()), maximum_pause)
    pauses: dict[int, int] = {}
    for index in sorted(pause_after):
        if index >= len(base_durations) or pause_budget <= 0:
            continue
        accepted = min(max(0, int(pause_after[index])), pause_budget)
        if accepted:
            pauses[index] = accepted
            pause_budget -= accepted
    active_days = duration_days - sum(pauses.values())
    active_durations = list(base_durations)
    excess = sum(active_durations) - active_days
    for index in range(len(active_durations) - 1, -1, -1):
        removable = max(0, active_durations[index] - 1)
        removed = min(removable, excess)
        active_durations[index] -= removed
        excess -= removed
    layout: list[tuple[date, date, bool, int]] = []
    cursor = start_date
    for active_index, size in enumerate(active_durations):
        block_end = cursor + timedelta(days=size - 1)
        layout.append((cursor, block_end, False, active_index))
        cursor = block_end + timedelta(days=1)
        pause_days = pauses.get(active_index, 0)
        if pause_days:
            pause_end = cursor + timedelta(days=pause_days - 1)
            layout.append((cursor, pause_end, True, active_index))
            cursor = pause_end + timedelta(days=1)
    return layout


def build_spend_block_rows(
    *,
    start_date: date,
    channel_totals: dict[int, float],
    cadence_days: int,
    style: str,
    pause_after: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Create budget-preserving, staggered spend blocks for any channel set."""

    if style not in {"flat", "balanced", "learning"}:
        raise ValueError("unknown spend-block style")
    layout = _block_layout(
        start_date=start_date,
        cadence_days=cadence_days,
        pause_after=pause_after or {},
    )
    active = [entry for entry in layout if not entry[2]]
    variation = {"flat": 0.0, "balanced": 0.24, "learning": 0.42}[style]
    patterns = (
        (0.78, 1.24, 0.88, 1.10),
        (1.18, 0.76, 1.16, 0.90),
        (0.84, 1.20, 0.72, 1.24),
        (1.22, 0.82, 1.08, 0.88),
    )
    daily: dict[int, list[float]] = {}
    for channel_index, (channel_id, total) in enumerate(channel_totals.items()):
        factors = [
            1.0
            + variation
            * (patterns[channel_index % len(patterns)][index % 4] - 1.0)
            for index in range(len(active))
        ]
        weighted_days = sum(
            factor * ((end - start).days + 1)
            for factor, (start, end, _, _) in zip(factors, active, strict=True)
        )
        base = float(total) / weighted_days if weighted_days else 0.0
        daily[channel_id] = [round(base * factor, 2) for factor in factors]
    rows: list[dict[str, Any]] = []
    for block_start, block_end, pause, active_index in layout:
        rows.append(
            {
                "start_date": block_start,
                "end_date": block_end,
                "pause": pause,
                "locked": False,
                "daily_spend": {
                    channel_id: 0.0 if pause else values[active_index]
                    for channel_id, values in daily.items()
                },
            }
        )
    return rows


def build_known_effect_experiment(
    observations: pd.DataFrame,
    channels: list[dict[str, Any]],
    *,
    learning_days: int = 42,
) -> dict[str, Any]:
    """Create fixed spend and matching known-lift sales for the local test."""

    if learning_days < 42 or learning_days % 14:
        raise ValueError("the known-effect test needs complete two-week periods")
    frame = observations.rename(columns={"date": "Date", "value": "Sales"}).copy()
    if not {"Date", "Sales"}.issubset(frame):
        raise ValueError("the known-effect test needs Date and Sales observations")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.date
    frame["Sales"] = pd.to_numeric(frame["Sales"], errors="coerce")
    frame = frame.dropna(subset=["Date", "Sales"]).sort_values("Date").reset_index(drop=True)
    enabled = [dict(item) for item in channels if bool(item.get("enabled", True))]
    if not enabled:
        raise ValueError("the known-effect test needs at least one enabled channel")

    selected: pd.DataFrame | None = None
    for start in range(0, len(frame) - learning_days + 1):
        candidate = frame.iloc[start : start + learning_days].copy()
        dates = list(candidate["Date"])
        if all(
            dates[index] == dates[0] + timedelta(days=index)
            for index in range(learning_days)
        ):
            # Preserve values, not the source-frame labels.  The known lift is
            # a zero-based series; retaining a later candidate's original
            # index would align by label and turn otherwise numeric sales into
            # missing values.
            selected = candidate.reset_index(drop=True)
            break
    if selected is None:
        raise ValueError(f"the known-effect test needs {learning_days} consecutive sales days")

    patterns = (
        (0, 28, 76, 12, 104, 44, 0, 88, 20, 116, 52, 8, 68, 36),
        (92, 16, 0, 64, 24, 108, 40, 4, 80, 32, 120, 12, 56, 72),
        (20, 96, 36, 0, 72, 16, 112, 48, 8, 60, 28, 104, 44, 4),
    )
    channel_ids = [int(item["channel_id"]) for item in enabled]
    daily_spend: dict[int, list[float]] = {}
    for channel_index, channel_id in enumerate(channel_ids):
        pattern = patterns[channel_index % len(patterns)]
        daily_spend[channel_id] = [
            float(pattern[(day + 3 * (day // 14)) % len(pattern)])
            for day in range(learning_days)
        ]

    adstock = {channel_id: 0.0 for channel_id in channel_ids}
    decay = {
        channel_id: max(0.20, 0.58 - 0.12 * index)
        for index, channel_id in enumerate(channel_ids)
    }
    response = {
        channel_id: max(2.6, 5.2 - 0.8 * index)
        for index, channel_id in enumerate(channel_ids)
    }
    lift: list[float] = []
    for day in range(learning_days):
        day_lift = 0.0
        for channel_id in channel_ids:
            adstock[channel_id] = (
                daily_spend[channel_id][day] + decay[channel_id] * adstock[channel_id]
            )
            saturated = adstock[channel_id] / (1.0 + adstock[channel_id] / 110.0)
            day_lift += response[channel_id] * saturated
        lift.append(round(day_lift, 2))

    sales = selected.loc[:, ["Date", "Sales"]].copy()
    sales["Sales"] = (sales["Sales"].astype(float) + pd.Series(lift)).round(2)
    plans: list[dict[str, Any]] = []
    first_day = selected.iloc[0]["Date"]
    for plan_index in range(learning_days // 14):
        plan_start = first_day + timedelta(days=14 * plan_index)
        blocks = []
        for offset in range(14):
            day_index = plan_index * 14 + offset
            spend_date = plan_start + timedelta(days=offset)
            blocks.append(
                {
                    "start_date": spend_date,
                    "end_date": spend_date,
                    "pause": all(
                        daily_spend[channel_id][day_index] == 0
                        for channel_id in channel_ids
                    ),
                    "locked": True,
                    "daily_spend": {
                        channel_id: daily_spend[channel_id][day_index]
                        for channel_id in channel_ids
                    },
                }
            )
        plans.append({"start_date": plan_start, "spend_blocks": blocks})
    return {
        "start_date": first_day,
        "end_date": first_day + timedelta(days=learning_days - 1),
        "sales": sales,
        "plans": plans,
        "known_lift": lift,
        "parameters": {"decay": decay, "response": response},
    }


def _render_setup_wizard(
    *,
    org_id: int,
    site_id: int,
    metric_id: int,
    actor_id: int,
) -> None:
    st.subheader("Set up marketing learning")
    st.write(
        "Choose the channels you use. Historical spend is optional: if you skip it, "
        "Learning turns on now and waits for your first two-week plan to be confirmed."
    )
    default_channels = pd.DataFrame(
        [
            {"Channel": "Search", "Type": "search"},
            {"Channel": "Social", "Type": "social"},
        ]
    )
    channels = st.data_editor(
        default_channels,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="mmm_setup_channels",
    )
    history_choice = st.radio(
        "Do you have confirmed historical spend?",
        ("No — start learning with future plans", "Yes — import historical spend"),
        horizontal=True,
    )
    upload = None
    if history_choice.startswith("Yes"):
        st.caption(
            "Upload CSV or Excel with one Date column and one daily-spend column "
            "for each channel named above. Zero-spend days should remain in the file."
        )
        upload = st.file_uploader(
            "Historical spend file",
            type=("csv", "xlsx", "xls"),
            key="mmm_historical_spend_upload",
        )
    development_mode = st.checkbox(
        "Enable local testing shortcut",
        value=True,
        help=(
            "Development only: lets this local app create and close a simulated "
            "completed two-week plan without waiting fourteen days."
        ),
    )
    if st.button("Turn on MMM Learning", type="primary"):
        try:
            channel_records = [
                {
                    "name": str(row["Channel"]).strip(),
                    "kind": str(row["Type"]).strip(),
                }
                for _, row in channels.dropna(how="all").iterrows()
                if str(row.get("Channel") or "").strip()
            ]
            history: list[dict[str, Any]] = []
            if history_choice.startswith("Yes"):
                if upload is None:
                    raise ValueError("choose a historical spend file or start without one")
                history = normalize_historical_spend(
                    _read_spend_upload(upload),
                    [item["name"] for item in channel_records],
                )
            repository = _pipeline_repository()
            try:
                result = repository.configure_mmm_learning(
                    site_id=site_id,
                    metric_id=metric_id,
                    created_by=actor_id,
                    channels=channel_records,
                    historical_spend=history,
                    development_closeout_enabled=development_mode,
                )
            finally:
                repository.close()
        except (ValueError, RuntimeError) as error:
            st.error(str(error).capitalize() + ".")
        else:
            if result["historical_rows"]:
                st.success("MMM Learning is on and the confirmed history is queued for learning.")
            else:
                st.success(
                    "MMM Learning is on. It will wait safely until a two-week plan "
                    "is confirmed as actual spend."
                )
            st.rerun()


def _render_plan_form(
    *,
    site_id: int,
    metric_id: int,
    channels: list[dict[str, Any]],
    actor_id: int,
    suggested_start: date,
) -> None:
    if "mmm_plan_start" not in st.session_state:
        st.session_state.mmm_plan_start = suggested_start
    start = st.date_input(
        "Plan starts",
        key="mmm_plan_start",
    )
    end = start + timedelta(days=13)
    st.caption(f"Plan ends {end.strftime('%b %d, %Y')}.")
    style_labels = {
        "Learn faster": "learning",
        "Balanced variation": "balanced",
        "Flat spending": "flat",
    }
    style_label = st.segmented_control(
        "Schedule style",
        list(style_labels),
        default="Learn faster",
        width="stretch",
    ) or "Learn faster"
    cadence = int(
        st.number_input(
            "Days per spending block",
            min_value=2,
            max_value=7,
            value=4,
            step=1,
        )
    )
    st.markdown("**Two-week spend by channel**")
    totals: dict[int, float] = {}
    columns = st.columns(min(2, len(channels)))
    for index, channel in enumerate(channels):
        with columns[index % len(columns)]:
            totals[int(channel["channel_id"])] = st.number_input(
                str(channel["name"]),
                min_value=0.0,
                step=50.0,
                key=f"mmm_plan_total_{channel['channel_id']}",
            )
    base_block_count = (14 + cadence - 1) // cadence
    pause_after: dict[int, int] = {}
    with st.expander("Add zero-spend pauses"):
        st.caption(
            "A pause is an explicit $0 block. Later spending blocks shift while "
            "the plan remains fourteen days long."
        )
        pause_columns = st.columns(min(2, base_block_count))
        for index in range(base_block_count):
            with pause_columns[index % len(pause_columns)]:
                days = int(
                    st.number_input(
                        f"After block {index + 1}",
                        min_value=0,
                        max_value=4,
                        value=0,
                        step=1,
                        key=f"mmm_pause_after_{index}",
                    )
                )
            if days:
                pause_after[index] = days
    generated = build_spend_block_rows(
        start_date=start,
        channel_totals=totals,
        cadence_days=cadence,
        style=style_labels[style_label],
        pause_after=pause_after,
    )
    editor_rows: list[dict[str, Any]] = []
    channel_by_name = {str(item["name"]): int(item["channel_id"]) for item in channels}
    for block in generated:
        editor_rows.append(
            {
                "Start": block["start_date"],
                "End": block["end_date"],
                "Pause": block["pause"],
                "Locked": block["locked"],
                **{
                    str(channel["name"]): block["daily_spend"][int(channel["channel_id"])]
                    for channel in channels
                },
            }
        )
    editor_key = (
        f"mmm_block_editor_{start}_{cadence}_{style_labels[style_label]}_"
        + "_".join(f"{key}-{value}" for key, value in sorted(pause_after.items()))
    )
    edited = st.data_editor(
        pd.DataFrame(editor_rows),
        hide_index=True,
        width="stretch",
        disabled=("Start", "End", "Pause"),
        column_config={
            "Start": st.column_config.DateColumn("Start"),
            "End": st.column_config.DateColumn("End"),
            "Pause": st.column_config.CheckboxColumn("Pause"),
            "Locked": st.column_config.CheckboxColumn("Lock"),
            **{
                name: st.column_config.NumberColumn(
                    name, min_value=0.0, step=5.0, format="$%.2f"
                )
                for name in channel_by_name
            },
        },
        key=editor_key,
    )
    block_budget = sum(
        sum(float(row[name]) for name in channel_by_name)
        * ((row["End"] - row["Start"]).days + 1)
        for _, row in edited.iterrows()
        if not bool(row["Pause"])
    )
    st.metric("Total planned spend", f"${block_budget:,.0f}")
    cancel_column, save_column = st.columns(2)
    if cancel_column.button("Cancel", width="stretch"):
        st.session_state.mmm_plan_drawer_open = False
        st.rerun()
    if save_column.button("Save plan", type="primary", width="stretch"):
        repository = _pipeline_repository()
        try:
            blocks: list[dict[str, Any]] = []
            for _, row in edited.iterrows():
                pause = bool(row["Pause"])
                blocks.append(
                    {
                        "start_date": pd.Timestamp(row["Start"]).date(),
                        "end_date": pd.Timestamp(row["End"]).date(),
                        "pause": pause,
                        "locked": bool(row["Locked"]),
                        "daily_spend": {
                            channel_id: 0.0 if pause else float(row[name])
                            for name, channel_id in channel_by_name.items()
                        },
                    }
                )
            repository.create_mmm_spend_plan(
                site_id=site_id,
                metric_id=metric_id,
                created_by=actor_id,
                start_date=start,
                spend_blocks=blocks,
            )
        except (ValueError, RuntimeError) as error:
            st.error(str(error).capitalize() + ".")
        else:
            st.session_state.mmm_plan_drawer_open = False
            st.session_state.mmm_plan_saved_notice = True
            st.rerun()
        finally:
            repository.close()


@st.dialog(
    "Plan marketing spend",
    width="small",
    dismissible=False,
    position="right",
)
def _plan_drawer(
    *,
    site_id: int,
    metric_id: int,
    channels: list[dict[str, Any]],
    actor_id: int,
    suggested_start: date,
) -> None:
    st.caption(
        "Build the next two-week plan. It will be anchored to the current "
        "forecast when saved."
    )
    _render_plan_form(
        site_id=site_id,
        metric_id=metric_id,
        channels=channels,
        actor_id=actor_id,
        suggested_start=suggested_start,
    )


def _render_plan_controls(
    *,
    org_id: int,
    site_id: int,
    metric_id: int,
    actor_id: int,
    state: dict[str, Any],
) -> None:
    channels = [item for item in state["channels"] if item["enabled"]]
    if not channels:
        st.warning("No marketing channels are enabled for this site.")
        return
    newest_end = max(
        (plan["end_date"] for plan in state["plans"]),
        default=date.today(),
    )
    suggested_start = max(date.today() + timedelta(days=1), newest_end + timedelta(days=1))
    if st.session_state.pop("mmm_plan_saved_notice", False):
        st.success(
            "The plan is saved and anchored to the current forecast. MMM Rescore "
            "will use it whenever an eligible fit is active."
        )
    if st.button("Create next plan", type="primary", key="open_mmm_plan_drawer"):
        st.session_state.mmm_plan_drawer_open = True
        st.session_state.mmm_plan_start = suggested_start
    if st.session_state.get("mmm_plan_drawer_open", False):
        _plan_drawer(
            site_id=site_id,
            metric_id=metric_id,
            channels=channels,
            actor_id=actor_id,
            suggested_start=suggested_start,
        )

    program_config = (state["program"] or {}).get("config") or {}
    if bool(program_config.get("development_closeout_enabled")):
        with st.expander("Local development test", expanded=False):
            st.warning(
                "This runs one fixed known-effect experiment. It creates three "
                "completed two-week plans, adds their known delayed marketing lift "
                "to the matching development sales dates, and queues a real Forecast "
                "Refresh. MMM Learning starts only after that forecast publishes."
            )
            st.caption(
                "The spending pattern and response are fixed so this is repeatable; "
                "there are no test-spend controls to tune toward a passing result."
            )
            prior_test = program_config.get("known_effect_test")
            if prior_test:
                st.info(
                    "The known-effect experiment has already been queued for this "
                    f"site as Forecast job #{prior_test.get('forecast_run_id')}."
                )
            if st.button(
                "Run the fixed known-effect test",
                disabled=bool(prior_test),
                type="primary",
            ):
                repository = _pipeline_repository()
                onboarding = _onboarding_repository()
                try:
                    if onboarding is None:
                        raise RuntimeError("the local data service is unavailable")
                    observations = _marketing_observations(
                        site_id=site_id, metric_id=metric_id
                    )
                    experiment = build_known_effect_experiment(
                        observations,
                        channels,
                    )
                    repository.clear_local_development_mmm_plans(
                        site_id=site_id,
                        metric_id=metric_id,
                        actor_id=actor_id,
                    )
                    plan_ids = [
                        repository.create_mmm_spend_plan(
                            site_id=site_id,
                            metric_id=metric_id,
                            created_by=actor_id,
                            start_date=plan["start_date"],
                            spend_blocks=plan["spend_blocks"],
                        )
                        for plan in experiment["plans"]
                    ]
                    sales = experiment["sales"]
                    filename = (
                        f"mmm-known-effect-{experiment['start_date']}-"
                        f"{experiment['end_date']}.csv"
                    )
                    payload = sales.to_csv(index=False).encode("utf-8")
                    update = onboarding.apply_observation_update(
                        site_id=site_id,
                        metric_id=metric_id,
                        rows=sales,
                        filename=filename,
                        payload=payload,
                        reason=(
                            "Local deterministic MMM known-effect experiment; "
                            "fixed spend, adstock, and response."
                        ),
                    )
                    for plan_id in plan_ids:
                        repository.closeout_mmm_spend_plan(
                            plan_id=plan_id,
                            confirmed_by=actor_id,
                            allow_early_for_development=True,
                            defer_until_next_forecast=True,
                        )
                    controller = _workflow_controller(repository)
                    if controller is None:
                        raise RuntimeError("the local forecast service is unavailable")
                    if update["load_kind"] == "correction":
                        run_id, _, _ = (
                            controller.enqueue_active_provenance_change_recovery(
                                int(update["review_id"]),
                                prior_dataset_id=int(update["prior_dataset_id"]),
                                replacement_dataset_id=int(update["dataset_id"]),
                                reason=(
                                    "Local deterministic MMM known-effect experiment"
                                ),
                                change_summary=dict(update["change_summary"]),
                                changed_rows=list(update["change_details"]),
                                requested_by=actor_id,
                            )
                        )
                    else:
                        run_id, _ = controller.enqueue_active_forecast_refresh(
                            int(update["review_id"]),
                            run_kind=str(update["load_kind"]),
                            change_summary=dict(update["change_summary"]),
                            requested_by=actor_id,
                        )
                    onboarding.link_forecast_refresh(int(update["review_id"]), run_id)
                    repository.record_local_known_effect_test(
                        site_id=site_id,
                        metric_id=metric_id,
                        actor_id=actor_id,
                        start_date=experiment["start_date"],
                        end_date=experiment["end_date"],
                        forecast_run_id=run_id,
                    )
                except Exception as error:
                    st.error(str(error).capitalize() + ".")
                else:
                    st.success(
                        f"Known-effect sales and spend are paired. Forecast job #{run_id} "
                        "is queued; MMM Learning will follow its publication."
                    )
                    st.rerun()
                finally:
                    if onboarding is not None:
                        onboarding.close()
                    repository.close()

    awaiting = [
        plan for plan in state["plans"]
        if plan.get("closeout_status") == "awaiting_confirmation"
    ]
    archived = [
        plan for plan in state["plans"]
        if plan.get("closeout_status") in {"confirmed", "corrected", "incomplete"}
    ][:3]
    reviewable = awaiting + archived
    if reviewable:
        st.subheader("Confirm actual spend")
        st.caption(
            "Confirm a completed block in one click, or open its daily editor for "
            "corrections and explicit zero-spend days. Learning starts only when "
            "confirmed spend and actual sales exist for the same dates. Recent "
            "archived plans remain available for audited corrections."
        )
        for plan in reviewable:
            archived_plan = plan.get("closeout_status") != "awaiting_confirmation"
            label = (
                f"Plan #{plan['id']} · {plan['start_date']} to {plan['end_date']} · "
                f"${float(plan['budget']):,.0f}"
            )
            if archived_plan:
                label += " · Archived"
            with st.container(border=True):
                st.markdown(f"**{label}**")
                st.caption(
                    f"Anchored to forecast run #{plan['anchor_forecast_run_id']} · "
                    f"{int(plan.get('confirmed_days') or 0)} of 14 days confirmed"
                )
                detail = _plan_daily_detail(int(plan["id"]), metric_id)
                if detail.empty:
                    st.warning("This plan has no spend blocks to review.")
                    continue
                channel_ids = {
                    str(channel["channel"]): int(channel["channel_id"])
                    for _, channel in detail[["channel", "channel_id"]].drop_duplicates().iterrows()
                }
                for sequence in sorted(detail["sequence"].unique()):
                    block = detail[detail["sequence"] == sequence].copy()
                    block_start = block["date"].min()
                    block_end = block["date"].max()
                    pause = bool(block["pause"].iloc[0])
                    day_count = int(block["date"].nunique())
                    confirmed_count = int(
                        block.groupby("date")["actual_spend"]
                        .apply(lambda values: values.notna().all())
                        .sum()
                    )
                    block_title = (
                        f"Block {int(sequence) + 1} · {block_start:%b %d}–"
                        f"{block_end:%b %d} · {confirmed_count}/{day_count} confirmed"
                    )
                    if pause:
                        block_title += " · planned pause"
                    with st.expander(block_title, expanded=False):
                        completed_end = min(date.today(), block_end)
                        next_day = (
                            plan["confirmed_through"] + timedelta(days=1)
                            if plan.get("confirmed_through") is not None
                            else plan["start_date"]
                        )
                        confirm_start = max(next_day, block_start)
                        can_confirm_block = (
                            not archived_plan
                            and
                            block_start <= next_day <= block_end
                            and confirm_start <= completed_end
                        )
                        sales_days = int(
                            block[block["date"] <= completed_end]
                            .groupby("date")["sales_available"]
                            .all()
                            .sum()
                        )
                        st.caption(
                            f"{sales_days} completed days currently have actual sales. "
                            "Confirmed spend without matching sales is saved safely and waits."
                        )
                        if st.button(
                            "Confirm completed days as planned",
                            key=f"confirm_block_{plan['id']}_{sequence}",
                            disabled=not can_confirm_block,
                            width="stretch",
                        ):
                            repository = _pipeline_repository()
                            try:
                                result = repository.confirm_mmm_spend(
                                    plan_id=int(plan["id"]),
                                    confirmed_by=actor_id,
                                    through_date=completed_end,
                                )
                            except (ValueError, RuntimeError) as error:
                                st.error(str(error).capitalize() + ".")
                            else:
                                if result["waiting_for_forecast"]:
                                    st.success(
                                        "Actual spend saved. Learning will begin only "
                                        "when matching sales are available."
                                    )
                                else:
                                    st.success("Actual spend saved and Learning notified.")
                                st.rerun()
                            finally:
                                repository.close()

                        eligible = block[block["date"] <= completed_end]
                        if archived_plan and not eligible.empty:
                            confirmed_dates = (
                                eligible.groupby("date")["actual_spend"]
                                .apply(lambda values: values.notna().all())
                            )
                            eligible = eligible[
                                eligible["date"].isin(confirmed_dates[confirmed_dates].index)
                            ]
                        if eligible.empty:
                            st.info(
                                "This block has no confirmed actual spend to correct."
                                if archived_plan
                                else "This block has not happened yet."
                            )
                            continue
                        planned = eligible.pivot_table(
                            index="date", columns="channel", values="planned_spend", aggfunc="first"
                        )
                        actual = eligible.pivot_table(
                            index="date", columns="channel", values="actual_spend", aggfunc="first"
                        )
                        sales = eligible.groupby("date")["sales_available"].all()
                        editor_rows = []
                        for spend_date in planned.index:
                            has_actual = spend_date in actual.index and actual.loc[spend_date].notna().all()
                            row = {
                                "Date": spend_date,
                                "Sales ready": bool(sales.loc[spend_date]),
                                "Treatment": "Enter actual" if has_actual else "As planned",
                            }
                            for channel_name in channel_ids:
                                row[channel_name] = float(
                                    actual.loc[spend_date, channel_name]
                                    if has_actual
                                    else planned.loc[spend_date, channel_name]
                                )
                            editor_rows.append(row)
                        st.caption(
                            "Use Unknown only for a genuinely unresolved day. It stays "
                            "out of Learning and never becomes a zero."
                        )
                        edited = st.data_editor(
                            pd.DataFrame(editor_rows),
                            hide_index=True,
                            width="stretch",
                            disabled=("Date", "Sales ready"),
                            column_config={
                                "Date": st.column_config.DateColumn("Date"),
                                "Sales ready": st.column_config.CheckboxColumn("Sales ready"),
                                "Treatment": st.column_config.SelectboxColumn(
                                    "Actual spend",
                                    options=("As planned", "Enter actual", "Zero", "Unknown"),
                                    required=True,
                                ),
                                **{
                                    name: st.column_config.NumberColumn(
                                        name, min_value=0.0, step=5.0, format="$%.2f"
                                    )
                                    for name in channel_ids
                                },
                            },
                            key=f"actual_editor_{plan['id']}_{sequence}",
                        )
                        if st.button(
                            "Save daily actuals",
                            key=f"save_actual_{plan['id']}_{sequence}",
                            width="stretch",
                        ):
                            days: list[dict[str, Any]] = []
                            unknown_reached = False
                            for _, row in edited.sort_values("Date").iterrows():
                                spend_date = pd.Timestamp(row["Date"]).date()
                                existing = actual.loc[spend_date].notna().all() if spend_date in actual.index else False
                                treatment = str(row["Treatment"])
                                if treatment == "Unknown":
                                    if existing:
                                        raise_error = (
                                            "A confirmed day cannot be changed back to unknown. "
                                            "Enter its corrected amount instead."
                                        )
                                        st.error(raise_error)
                                        days = []
                                    unknown_reached = True
                                    continue
                                if unknown_reached and not existing:
                                    continue
                                if treatment == "As planned":
                                    amounts = {
                                        channel_ids[name]: float(planned.loc[spend_date, name])
                                        for name in channel_ids
                                    }
                                elif treatment == "Zero":
                                    amounts = {channel_id: 0.0 for channel_id in channel_ids.values()}
                                else:
                                    amounts = {
                                        channel_ids[name]: float(row[name])
                                        for name in channel_ids
                                    }
                                days.append({"date": spend_date, "actual_spend": amounts})
                            if days:
                                repository = _pipeline_repository()
                                try:
                                    results = repository.record_mmm_spend_days(
                                        plan_id=int(plan["id"]),
                                        confirmed_by=actor_id,
                                        days=days,
                                    )
                                except (ValueError, RuntimeError) as error:
                                    st.error(str(error).capitalize() + ".")
                                else:
                                    changed = [item for item in results if item["status"] != "unchanged"]
                                    waiting = any(item.get("waiting_for_forecast") for item in changed)
                                    if waiting:
                                        st.success(
                                            "Actual spend saved. Dates without sales are "
                                            "waiting and did not start Learning."
                                        )
                                    else:
                                        st.success("Actual spend saved and Learning notified.")
                                    st.rerun()
                                finally:
                                    repository.close()

                if archived_plan:
                    continue
                plan_complete = int(plan.get("confirmed_days") or 0) >= 14
                plan_exhausted = plan["end_date"] <= date.today()
                if plan_complete:
                    st.caption("All plan days are resolved. Archive it to open the next plan.")
                elif plan_exhausted:
                    st.warning(
                        "This plan has ended with unresolved days. Resolve them above, "
                        "or explicitly archive them as unknown."
                    )
                acknowledge_unknown = False
                if plan_exhausted and not plan_complete:
                    acknowledge_unknown = st.checkbox(
                        "Archive the remaining days as unknown — not zero",
                        key=f"unknown_closeout_{plan['id']}",
                    )
                if (plan_complete or plan_exhausted) and st.button(
                    "Archive plan and create the next one",
                    key=f"closeout_{plan['id']}",
                    disabled=not plan_complete and not acknowledge_unknown,
                    width="stretch",
                ):
                    repository = _pipeline_repository()
                    try:
                        repository.closeout_mmm_spend_plan(
                            plan_id=int(plan["id"]),
                            confirmed_by=actor_id,
                            archive_incomplete=not plan_complete,
                        )
                    except (ValueError, RuntimeError) as error:
                        st.error(str(error).capitalize() + ".")
                    else:
                        st.session_state.mmm_plan_drawer_open = True
                        st.session_state.mmm_plan_start = plan["end_date"] + timedelta(days=1)
                        st.rerun()
                    finally:
                        repository.close()


def render_marketing_plan_drawer(
    *,
    org_id: int,
    site_id: int,
    metric_id: int,
    actor_id: int,
) -> None:
    """Render the database-backed future-spend planner beside Forecast."""

    repository = _pipeline_repository()
    try:
        state = repository.mmm_setup_state(site_id=site_id, metric_id=metric_id)
    finally:
        repository.close()

    if state["program"] is None:
        st.warning(
            "Turn on Marketing Learning in the Marketing workspace before creating "
            "a spend plan."
        )
        return

    channels = [item for item in state["channels"] if item["enabled"]]
    if not channels:
        st.warning("No marketing channels are enabled for this site.")
        return

    newest_end = max(
        (plan["end_date"] for plan in state["plans"]),
        default=date.today(),
    )
    suggested_start = max(
        date.today() + timedelta(days=1),
        newest_end + timedelta(days=1),
    )
    if "mmm_plan_start" not in st.session_state:
        st.session_state.mmm_plan_start = suggested_start
    _plan_drawer(
        site_id=site_id,
        metric_id=metric_id,
        channels=channels,
        actor_id=actor_id,
        suggested_start=suggested_start,
    )


def render_database_marketing_workspace(
    *,
    org_id: int,
    site_id: int,
    metric_id: int,
    site_name: str,
    actor_id: int,
    can_override: bool,
) -> None:
    """Render setup, learning status, plans, and closeout for one site."""

    st.title("Marketing")
    st.caption(f"{site_name} · site-specific marketing learning")
    repository = _pipeline_repository()
    try:
        state = repository.mmm_setup_state(site_id=site_id, metric_id=metric_id)
    finally:
        repository.close()
    if state["program"] is None:
        _render_setup_wizard(
            org_id=org_id,
            site_id=site_id,
            metric_id=metric_id,
            actor_id=actor_id,
        )
        return

    program = state["program"]
    history = state["historical_spend"]
    col1, col2, col3 = st.columns(3)
    col1.metric("MMM state", str(program["lifecycle"]).replace("_", " ").title())
    col2.metric("Confirmed spend days", int(history["day_count"] or 0))
    col3.metric("Confirmed spend", f"${float(history['total'] or 0):,.0f}")
    operational = _current_operational_summary(site_id=site_id, metric_id=metric_id)
    if operational is not None:
        with st.container(border=True):
            st.subheader("Current marketing influence")
            st.caption(
                f"Published by MMM Rescore job #{operational['scoring_run_id']} "
                f"against forecast run #{operational['source_forecast_run_id']} and "
                f"fit #{operational['qualification_id']}."
            )
            score_columns = st.columns(4)
            score_columns[0].metric(
                "Organic baseline",
                f"${float(operational['organic_total'] or 0):,.0f}",
            )
            score_columns[1].metric(
                "Existing carryover",
                f"${float(operational['carryover'] or 0):,.0f}",
            )
            score_columns[2].metric(
                "Planned lift",
                f"${float(operational['planned_lift'] or 0):,.0f}",
            )
            score_columns[3].metric(
                "Scored forecast",
                f"${float(operational['scored_total'] or 0):,.0f}",
            )
            if operational["plan_id"] is None:
                st.info(
                    "No future plan is attached. The visible marketing influence is "
                    "carryover from already executed spend."
                )
            else:
                planned_spend = float(operational["planned_spend"] or 0)
                planned_lift = float(operational["planned_lift"] or 0)
                average_roas = planned_lift / planned_spend if planned_spend else 0.0
                st.caption(
                    f"Implemented plan #{operational['plan_id']} · "
                    f"${planned_spend:,.0f} planned spend · "
                    f"{average_roas:.2f}× visible-horizon ROAS · "
                    f"{str(operational['health_status']).title()} model health"
                )
    if not history["day_count"]:
        st.info(
            "Learning is enabled but waiting for confirmed spend. Create a plan now; "
            "confirm completed days whenever actual spend is available."
        )
    elif state["qualification"] is None:
        st.info("Confirmed spend is available. The next Learning run will evaluate it.")
    else:
        decision = str(state["qualification"]["decision"])
        st.info(f"Latest Learning decision: {decision.title()}.")
        summary = dict(state["qualification"].get("summary") or {})
        reasons = [
            dict(item)
            for item in summary.get("failed_reasons", [])
            if isinstance(item, dict)
        ]
        if reasons:
            with st.expander("Why MMM is still learning", expanded=False):
                st.caption(
                    "These are evidence checks, not job errors. They remain in the "
                    "Learning history so spending design and acceptance criteria can "
                    "be reviewed later."
                )
                labels = {
                    "sampling_health": "Sampling health",
                    "holdout_predictive_value": "Time holdout value",
                    "rolling_window_stability": "Rolling-window stability",
                    "upstream_world_robustness": "Upstream-world robustness",
                    "response_plausibility": "Response plausibility",
                    "aggregate_identification": "Aggregate identification",
                    "operational_coverage": "Channel coverage",
                    "eligibility_policy_approved": "Eligibility policy approved",
                }
                rows = []
                for reason in reasons:
                    observed = reason.get("observed")
                    threshold = reason.get("threshold")
                    rows.append(
                        {
                            "Check": labels.get(
                                str(reason.get("code")),
                                str(reason.get("code", "Evidence check")).replace("_", " ").title(),
                            ),
                            "Observed": "Not available" if observed is None else observed,
                            "Required": "Not set" if threshold is None else threshold,
                            "What helps": reason.get("suggested_action") or "No action needed",
                        }
                    )
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if state.get("pending_activation") is not None:
        pending = state["pending_activation"]
        refresh_status = str(pending.get("refresh_status") or "pending").replace(
            "_", " "
        )
        st.info(
            "A fit is awaiting guarded activation. Its marketing-clean baseline "
            f"rebuild is {refresh_status}; the existing forecast remains current "
            "until MMM Rescore also succeeds."
        )
    qualification_history = _qualification_history(
        site_id=site_id, metric_id=metric_id
    )
    if qualification_history:
        with st.expander("Learning and eligibility history", expanded=False):
            history_rows = []
            for item in qualification_history:
                summary = dict(item.get("summary") or {})
                failed = [
                    str(reason.get("code", "check")).replace("_", " ").title()
                    for reason in summary.get("failed_reasons", [])
                    if isinstance(reason, dict)
                ]
                outcome = str(item["decision"]).title()
                if item.get("override_id") is not None:
                    outcome += " · administrator override"
                history_rows.append(
                    {
                        "Decided": item["decided_at"],
                        "Learning job": int(item["learning_run_id"]),
                        "Outcome": outcome,
                        "State": str(item["state"]).replace("_", " ").title(),
                        "Checks needing attention": ", ".join(failed) or "None",
                        "Override reason": item.get("override_reason") or "",
                    }
                )
            st.dataframe(
                pd.DataFrame(history_rows), hide_index=True, width="stretch"
            )
    development_mode = bool(
        dict(program.get("config") or {}).get("development_closeout_enabled")
    )
    qualification = state.get("qualification")
    if (
        can_override
        and development_mode
        and qualification is not None
        and str(qualification.get("decision")) == "learning"
        and str(program.get("lifecycle")) == "learning"
    ):
        with st.expander("Local administrator override", expanded=False):
            st.warning(
                "This does not change the scientific Learning decision. It authorizes "
                "a controlled operational trial and still requires the marketing-clean "
                "baseline rebuild and MMM Rescore to pass before anything goes live."
            )
            override_reason = st.text_area(
                "Reason for the override",
                key="mmm_override_reason",
                placeholder=(
                    "Explain why an operational trial is justified despite the "
                    "failed eligibility checks."
                ),
            )
            if st.button(
                "Authorize controlled operational trial",
                disabled=len(override_reason.strip()) < 12,
                key="approve_mmm_override",
            ):
                repository = _pipeline_repository()
                try:
                    override_id = repository.approve_mmm_operational_override(
                        program_id=int(program["id"]),
                        qualification_id=int(qualification["id"]),
                        approved_by=actor_id,
                        reason=override_reason,
                    )
                except (ValueError, RuntimeError, PermissionError) as error:
                    st.error(str(error).capitalize() + ".")
                else:
                    st.success(
                        f"Override #{override_id} is recorded. The guarded baseline "
                        "rebuild and Rescore activation are queued."
                    )
                    st.rerun()
                finally:
                    repository.close()
    _render_plan_controls(
        org_id=org_id,
        site_id=site_id,
        metric_id=metric_id,
        actor_id=actor_id,
        state=state,
    )
