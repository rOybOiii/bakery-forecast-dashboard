"""Document-backed marketing plans and deterministic MVP scenario scoring.

The dashboard owns interaction state, while this module owns the durable plan
contract and the inference-free preview calculation.  A future MMM adapter can
replace ``score_plan`` without changing the Streamlit page contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Iterable
from uuid import uuid4

import pandas as pd


PLAN_SCHEMA_VERSION = 1
CHANNEL_ORDER = ("search", "social", "radio")


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    label: str
    starting_roas: float
    daily_saturation: float
    adstock_decay: float


CHANNEL_SPECS = {
    "search": ChannelSpec("search", "Search", 3.10, 250.0, 0.22),
    "social": ChannelSpec("social", "Social", 2.65, 185.0, 0.46),
    "radio": ChannelSpec("radio", "Radio", 2.30, 135.0, 0.68),
}


@dataclass(frozen=True)
class ChannelPlan:
    eligible: bool
    amount: float


@dataclass(frozen=True)
class MarketingPlan:
    plan_id: str
    restaurant: str
    start_date: str
    end_date: str
    total_budget: float
    allocation_mode: str
    channels: dict[str, ChannelPlan]
    baseline_vintage: str
    mmm_fit_version: str
    updated_at: str
    author: str

    @property
    def start(self) -> date:
        return date.fromisoformat(self.start_date)

    @property
    def end(self) -> date:
        return date.fromisoformat(self.end_date)

    @property
    def duration_days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class MarketingPlanState:
    schema_version: int
    published_plan: MarketingPlan | None
    draft_plan: MarketingPlan | None


@dataclass(frozen=True)
class ScenarioSummary:
    organic_total: float
    expected_total: float
    marketing_lift: float
    spend: float
    average_roas: float
    marginal_roas: float
    carryover_beyond_horizon: float


@dataclass(frozen=True)
class ScenarioResult:
    daily: pd.DataFrame
    channel_daily: pd.DataFrame
    summary: ScenarioSummary
    saturation: dict[str, str]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_state() -> MarketingPlanState:
    return MarketingPlanState(
        schema_version=PLAN_SCHEMA_VERSION,
        published_plan=None,
        draft_plan=None,
    )


def _plan_from_dict(raw: dict | None) -> MarketingPlan | None:
    if raw is None:
        return None
    channels = {
        key: ChannelPlan(
            eligible=bool(value["eligible"]),
            amount=float(value["amount"]),
        )
        for key, value in raw["channels"].items()
    }
    return MarketingPlan(
        plan_id=str(raw["plan_id"]),
        restaurant=str(raw["restaurant"]),
        start_date=str(raw["start_date"]),
        end_date=str(raw["end_date"]),
        total_budget=float(raw["total_budget"]),
        allocation_mode=str(raw["allocation_mode"]),
        channels=channels,
        baseline_vintage=str(raw["baseline_vintage"]),
        mmm_fit_version=str(raw["mmm_fit_version"]),
        updated_at=str(raw["updated_at"]),
        author=str(raw["author"]),
    )


def load_plan_state(path: str | Path) -> MarketingPlanState:
    source = Path(path)
    if not source.exists():
        return empty_state()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if int(raw.get("schema_version", -1)) != PLAN_SCHEMA_VERSION:
        raise ValueError("Unsupported marketing plan document schema")
    state = MarketingPlanState(
        schema_version=PLAN_SCHEMA_VERSION,
        published_plan=_plan_from_dict(raw.get("published_plan")),
        draft_plan=_plan_from_dict(raw.get("draft_plan")),
    )
    for plan in (state.published_plan, state.draft_plan):
        if plan is not None:
            validate_plan(plan, allow_past=True)
    return state


def save_plan_state(path: str | Path, state: MarketingPlanState) -> None:
    """Atomically replace the small single-user MVP plan document."""

    if state.schema_version != PLAN_SCHEMA_VERSION:
        raise ValueError("Unsupported marketing plan document schema")
    for plan in (state.published_plan, state.draft_plan):
        if plan is not None:
            validate_plan(plan, allow_past=True)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = {
        "schema_version": state.schema_version,
        "published_plan": asdict(state.published_plan)
        if state.published_plan
        else None,
        "draft_plan": asdict(state.draft_plan) if state.draft_plan else None,
    }
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def validate_plan(
    plan: MarketingPlan,
    *,
    today: date | None = None,
    allow_past: bool = False,
) -> None:
    if plan.restaurant != "downtown":
        raise ValueError("The MVP marketing plan is qualified only for Downtown")
    if plan.allocation_mode not in {"optimized", "manual"}:
        raise ValueError("allocation_mode must be optimized or manual")
    if plan.end < plan.start:
        raise ValueError("Marketing plan end date precedes its start date")
    if not allow_past and plan.start < (today or date.today()):
        raise ValueError("Marketing plans cannot begin in the past")
    if plan.total_budget < 0:
        raise ValueError("Marketing budget cannot be negative")
    if set(plan.channels) != set(CHANNEL_ORDER):
        raise ValueError("Marketing plan channel set is incomplete")
    if any(channel.amount < 0 for channel in plan.channels.values()):
        raise ValueError("Channel amounts cannot be negative")
    if any(
        not channel.eligible and abs(channel.amount) > 0.009
        for channel in plan.channels.values()
    ):
        raise ValueError("Ineligible channels must have zero allocation")
    allocated = sum(channel.amount for channel in plan.channels.values())
    if abs(allocated - plan.total_budget) > 0.02:
        raise ValueError("Channel allocations must equal total budget")
    if plan.total_budget > 0 and not any(
        channel.eligible for channel in plan.channels.values()
    ):
        raise ValueError("A positive budget requires an eligible channel")


def saturation_thresholds(channel: str, duration_days: int) -> tuple[float, float]:
    spec = CHANNEL_SPECS[channel]
    yellow = spec.daily_saturation * duration_days * 0.72
    red = spec.daily_saturation * duration_days * 1.18
    return yellow, red


def saturation_zone(channel: str, amount: float, duration_days: int) -> str:
    yellow, red = saturation_thresholds(channel, duration_days)
    if amount >= red:
        return "red"
    if amount >= yellow:
        return "yellow"
    return "green"


def _channel_total_lift(channel: str, amount: float, duration_days: int) -> float:
    spec = CHANNEL_SPECS[channel]
    if amount <= 0:
        return 0.0
    daily_spend = amount / max(duration_days, 1)
    daily_lift = (
        daily_spend
        * spec.starting_roas
        / (1.0 + daily_spend / spec.daily_saturation)
    )
    return daily_lift * duration_days


def optimize_allocations(
    total_budget: float,
    eligible_channels: Iterable[str],
    duration_days: int,
    *,
    increment: float = 25.0,
) -> dict[str, float]:
    eligible = [key for key in CHANNEL_ORDER if key in set(eligible_channels)]
    if total_budget < 0:
        raise ValueError("Marketing budget cannot be negative")
    if total_budget > 0 and not eligible:
        raise ValueError("Select at least one eligible channel")
    allocation = {key: 0.0 for key in CHANNEL_ORDER}
    remaining = round(float(total_budget), 2)
    while remaining > 0.009:
        step = min(increment, remaining)
        best = max(
            eligible,
            key=lambda key: _channel_total_lift(
                key, allocation[key] + step, duration_days
            )
            - _channel_total_lift(key, allocation[key], duration_days),
        )
        allocation[best] = round(allocation[best] + step, 2)
        remaining = round(remaining - step, 2)
    return allocation


def make_plan(
    *,
    restaurant: str,
    start_date: date,
    end_date: date,
    total_budget: float,
    eligible: dict[str, bool],
    allocation_mode: str,
    allocations: dict[str, float] | None,
    baseline_vintage: str,
    mmm_fit_version: str,
    author: str = "MVP manager",
    plan_id: str | None = None,
) -> MarketingPlan:
    duration = (end_date - start_date).days + 1
    if allocation_mode == "optimized":
        values = optimize_allocations(
            total_budget,
            [key for key, is_eligible in eligible.items() if is_eligible],
            duration,
        )
    else:
        values = {
            key: round(float((allocations or {}).get(key, 0.0)), 2)
            for key in CHANNEL_ORDER
        }
    channels = {
        key: ChannelPlan(bool(eligible.get(key, False)), values[key])
        for key in CHANNEL_ORDER
    }
    plan = MarketingPlan(
        plan_id=plan_id or str(uuid4()),
        restaurant=restaurant,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        total_budget=round(float(total_budget), 2),
        allocation_mode=allocation_mode,
        channels=channels,
        baseline_vintage=baseline_vintage,
        mmm_fit_version=mmm_fit_version,
        updated_at=utc_now_iso(),
        author=author,
    )
    validate_plan(plan)
    return plan


def zero_plan(
    *,
    restaurant: str,
    start_date: date,
    days: int,
    baseline_vintage: str,
    mmm_fit_version: str,
) -> MarketingPlan:
    return make_plan(
        restaurant=restaurant,
        start_date=start_date,
        end_date=start_date + timedelta(days=days - 1),
        total_budget=0.0,
        eligible={key: True for key in CHANNEL_ORDER},
        allocation_mode="optimized",
        allocations=None,
        baseline_vintage=baseline_vintage,
        mmm_fit_version=mmm_fit_version,
    )


def initialize_draft(
    state: MarketingPlanState,
    *,
    restaurant: str,
    start_date: date,
    baseline_vintage: str,
    mmm_fit_version: str,
) -> MarketingPlan:
    """Saved draft first, then published plan, then a zero-budget plan."""

    if state.draft_plan is not None:
        return state.draft_plan
    if state.published_plan is not None:
        published = state.published_plan
        duration = published.duration_days
        shifted_end = start_date + timedelta(days=duration - 1)
        return replace(
            published,
            plan_id=str(uuid4()),
            start_date=start_date.isoformat(),
            end_date=shifted_end.isoformat(),
            updated_at=utc_now_iso(),
        )
    return zero_plan(
        restaurant=restaurant,
        start_date=start_date,
        days=14,
        baseline_vintage=baseline_vintage,
        mmm_fit_version=mmm_fit_version,
    )


def save_draft(state: MarketingPlanState, plan: MarketingPlan) -> MarketingPlanState:
    validate_plan(plan)
    return replace(state, draft_plan=replace(plan, updated_at=utc_now_iso()))


def implement_plan(
    state: MarketingPlanState, plan: MarketingPlan
) -> MarketingPlanState:
    validate_plan(plan)
    return replace(
        state,
        published_plan=replace(plan, updated_at=utc_now_iso()),
        draft_plan=None,
    )


def _daily_channel_effect(
    channel: str,
    amount: float,
    plan: MarketingPlan,
    dates: pd.DatetimeIndex,
) -> list[float]:
    spec = CHANNEL_SPECS[channel]
    daily_spend = amount / max(plan.duration_days, 1)
    direct = (
        daily_spend
        * spec.starting_roas
        / (1.0 + daily_spend / spec.daily_saturation)
    )
    stock = 0.0
    effects: list[float] = []
    for timestamp in dates:
        active = plan.start <= timestamp.date() <= plan.end
        stock = direct + spec.adstock_decay * stock if active else spec.adstock_decay * stock
        effects.append((1.0 - spec.adstock_decay) * stock)
    return effects


def score_plan(baseline: pd.DataFrame, plan: MarketingPlan) -> ScenarioResult:
    """Score a plan without fitting or mutating the MMM.

    This deterministic response/adstock engine is an MVP stand-in.  Its output
    columns are the stable contract expected from the future frozen-MMM adapter.
    """

    validate_plan(plan, allow_past=True)
    required = {"target_date", "median", "q05", "q95"}
    if not required.issubset(baseline.columns):
        raise ValueError(f"Baseline is missing fields: {sorted(required - set(baseline))}")
    frame = baseline.loc[:, ["target_date", "median", "q05", "q95"]].copy()
    frame["target_date"] = pd.to_datetime(frame["target_date"])
    frame = frame.sort_values("target_date").reset_index(drop=True)
    horizon_end = frame["target_date"].max().date()
    calculation_end = max(horizon_end + timedelta(days=35), plan.end + timedelta(days=35))
    dates = pd.date_range(frame["target_date"].min(), calculation_end, freq="D")
    channel_daily = pd.DataFrame({"target_date": dates})
    for channel in CHANNEL_ORDER:
        channel_daily[channel] = _daily_channel_effect(
            channel,
            plan.channels[channel].amount,
            plan,
            dates,
        )
    channel_daily["marketing_lift"] = channel_daily[list(CHANNEL_ORDER)].sum(axis=1)
    visible = channel_daily.loc[
        channel_daily["target_date"].le(pd.Timestamp(horizon_end))
    ].copy()
    frame = frame.merge(visible, on="target_date", how="left")
    frame[list(CHANNEL_ORDER) + ["marketing_lift"]] = frame[
        list(CHANNEL_ORDER) + ["marketing_lift"]
    ].fillna(0.0)
    marketing_sd = 0.25 * frame["marketing_lift"]
    frame["operational_median"] = frame["median"] + frame["marketing_lift"]
    frame["operational_q05"] = frame["q05"] + 0.65 * frame["marketing_lift"] - marketing_sd
    frame["operational_q95"] = frame["q95"] + 1.35 * frame["marketing_lift"] + marketing_sd
    carryover = channel_daily.loc[
        channel_daily["target_date"].gt(pd.Timestamp(horizon_end)),
        "marketing_lift",
    ].sum()
    visible_lift = float(frame["marketing_lift"].sum())
    spend = float(plan.total_budget)
    average_roas = visible_lift / spend if spend else 0.0
    epsilon = max(25.0, spend * 0.01)
    eligible = [key for key, value in plan.channels.items() if value.eligible]
    if eligible:
        marginal_roas = max(
            (
                _channel_total_lift(
                    key,
                    plan.channels[key].amount + epsilon,
                    plan.duration_days,
                )
                - _channel_total_lift(
                    key,
                    plan.channels[key].amount,
                    plan.duration_days,
                )
            )
            / epsilon
            for key in eligible
        )
    else:
        marginal_roas = 0.0
    summary = ScenarioSummary(
        organic_total=float(frame["median"].sum()),
        expected_total=float(frame["operational_median"].sum()),
        marketing_lift=visible_lift,
        spend=spend,
        average_roas=average_roas,
        marginal_roas=marginal_roas,
        carryover_beyond_horizon=float(carryover),
    )
    saturation = {
        key: saturation_zone(key, plan.channels[key].amount, plan.duration_days)
        for key in CHANNEL_ORDER
    }
    return ScenarioResult(
        daily=frame,
        channel_daily=channel_daily,
        summary=summary,
        saturation=saturation,
    )


def plans_equivalent(left: MarketingPlan, right: MarketingPlan) -> bool:
    """Compare manager-editable plan fields while ignoring identity metadata."""

    return (
        left.restaurant == right.restaurant
        and left.start_date == right.start_date
        and left.end_date == right.end_date
        and abs(left.total_budget - right.total_budget) < 0.01
        and left.allocation_mode == right.allocation_mode
        and all(
            left.channels[key] == right.channels[key] for key in CHANNEL_ORDER
        )
        and left.baseline_vintage == right.baseline_vintage
        and left.mmm_fit_version == right.mmm_fit_version
    )
