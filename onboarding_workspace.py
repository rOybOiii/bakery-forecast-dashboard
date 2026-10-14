"""Database-backed first-data-import wizard for local Credent development."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st


ROOT = Path(__file__).resolve().parent
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DECISION_LABELS = {
    "Choose…": None,
    "Keep original": "keep",
    "Ignore value": "ignore",
    "Treat as missing": "missing",
    "Mark closed": "closed",
    "Replace value": "replace",
}
DECISION_CODES = {value: key for key, value in DECISION_LABELS.items() if value}
SITE_STATE_LABELS = {
    "needs_data": "Needs data",
    "reviewing": "Data review in progress",
    "ready_for_forecast": "Ready for first forecast",
    "forecasting": "First forecast running",
    "active": "Forecasting active",
}
SCHEDULE_MISMATCH_TYPES = {"unexpected_open", "unexpected_closed"}


def _services():
    try:
        from credent_onboarding import (
            OnboardingRepository,
            inspect_daily_data,
            parsed_daily_preview,
            select_daily_columns,
        )
    except ImportError:
        return None, None, None, None
    return OnboardingRepository, inspect_daily_data, parsed_daily_preview, select_daily_columns


def _repository():
    repository_class, *_ = _services()
    return (
        repository_class(artifact_dir=ROOT.parent / ".credent-artifacts")
        if repository_class
        else None
    )


def _pipeline_repository():
    try:
        from credent_pipeline import PipelineRepository
    except ImportError:
        local_source = ROOT.parent / "Credent Core" / "packages" / "pipeline" / "src"
        if local_source.exists() and str(local_source) not in sys.path:
            sys.path.insert(0, str(local_source))
        try:
            from credent_pipeline import PipelineRepository
        except ImportError:
            return None
    return PipelineRepository()


def _workflow_controller(pipeline):
    """Build the enqueue-only controller used by the Streamlit request path."""

    try:
        from credent_pipeline import (
            WorkflowController,
            build_local_step_handler_registry,
        )
    except ImportError:
        return None
    return WorkflowController(
        pipeline,
        build_local_step_handler_registry(),
        worker_id=None,
    )


def _site_context(site: dict[str, Any]) -> dict[str, Any]:
    return {
        "org_id": int(site["org_id"]),
        "site_id": int(site["site_id"]),
        "metric_id": int(site["metric_id"]),
        "review_id": site.get("draft_review_id") or site.get("review_id"),
        "approved_review_id": site.get("approved_review_id"),
        "first_import": bool(site.get("first_import")),
    }


def _stage_label(stage: object) -> str:
    labels = {
        "preflight": "Checking the modeling environment",
        "detrend": "Estimating the baseline",
        "pprm_batch": "Finding persistent sales regimes",
        "pprm_online": "Updating persistent sales regimes",
        "pprm_compare": "Comparing regime estimates",
        "jeam": "Estimating event effects",
        "jeam_pass1": "Finding cross-site event support",
        "jeam_pass2": "Estimating event effects",
        "erp": "Building recurring-event patterns",
        "mmm": "Estimating marketing response",
        "forecast": "Building the forecast",
        "validate": "Checking the forecast",
        "validate_first_forecast": "Checking the forecast",
        "publish": "Publishing the forecast",
    }
    value = str(stage or "waiting")
    return labels.get(value, value.replace("_", " ").title())


def _elapsed_label(started_at: object, ended_at: object) -> str:
    if started_at is None:
        return "—"
    start = pd.Timestamp(started_at)
    end = pd.Timestamp(ended_at) if ended_at is not None else pd.Timestamp.now(tz=start.tz)
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def render_run_details(run_id: int, *, expanded: bool = False) -> None:
    """Show a manager-readable account of one forecast pipeline run."""

    pipeline = _pipeline_repository()
    if pipeline is None:
        return
    try:
        summary = pipeline.run_summary(run_id)
    except Exception as error:
        st.warning(f"Forecast run details are temporarily unavailable: {error}")
        return
    finally:
        pipeline.close()

    run = summary["run"]
    status = str(run["status"]).replace("_", " ").title()
    with st.expander(f"Forecast run #{run_id} details · {status}", expanded=expanded):
        header = st.columns(3)
        header[0].metric("Run", f"#{run_id}")
        header[1].metric("Status", status)
        header[2].metric(
            "Elapsed",
            _elapsed_label(run.get("started_at"), run.get("ended_at")),
        )

        diagnostics_by_step = {
            int(item["step_id"]): item
            for item in summary["diagnostics"]
            if item.get("step_id") is not None
        }
        attempts_by_step: dict[int, list[dict[str, Any]]] = {}
        for attempt in summary.get("attempts", ()):
            attempts_by_step.setdefault(int(attempt["step_id"]), []).append(attempt)
        rows = []
        for step in summary["steps"]:
            diagnostic = diagnostics_by_step.get(int(step["id"]))
            step_attempts = attempts_by_step.get(int(step["id"]), ())
            rows.append(
                {
                    "Stage": _stage_label(step["stage"]),
                    "Status": str(step["status"]).replace("_", " ").title(),
                    "Attempts": len(step_attempts) or int(step.get("attempt") or 0),
                    "Elapsed": _elapsed_label(step.get("started_at"), step.get("ended_at")),
                    "Checks": (
                        str(diagnostic["status"]).title()
                        if diagnostic is not None
                        else "—"
                    ),
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

        for step in summary["steps"]:
            if step.get("error_text"):
                st.error(f"{_stage_label(step['stage'])}: {step['error_text']}")

        retries = [
            attempt
            for attempt in summary.get("attempts", ())
            if attempt.get("status") == "retrying"
        ]
        if retries:
            st.markdown("**Automatic retries**")
            for attempt in retries:
                target = attempt.get("retry_from_step") or attempt["stage"]
                reason = str(
                    attempt.get("error_text")
                    or attempt.get("error_code")
                    or "the step did not pass"
                )
                st.caption(
                    f"{_stage_label(attempt['stage'])}, attempt {attempt['attempt']}: "
                    f"{reason}. Resumed from {_stage_label(target)}."
                )

        if summary["diagnostics"]:
            st.markdown("**Diagnostic readouts**")
            for diagnostic in summary["diagnostics"]:
                phase = _stage_label(diagnostic["phase"])
                failed = int(diagnostic.get("failed_checks") or 0)
                warnings = int(diagnostic.get("warning_checks") or 0)
                count = int(diagnostic.get("check_count") or 0)
                st.caption(
                    f"{phase}: {str(diagnostic['status']).title()} · "
                    f"{count} checks · {warnings} warnings · {failed} failures"
                )

        durable_artifacts = [
            str(artifact["kind"]).replace("_", " ").title()
            for artifact in summary["artifacts"]
            if not str(artifact["kind"]).startswith("simulated_")
        ]
        if durable_artifacts:
            st.caption("Saved outputs: " + ", ".join(durable_artifacts))


def render_data_validity_controls(site: dict[str, Any]) -> None:
    """Offer an explicit, audited invalidation action for the site's approved data."""

    dataset_id = site.get("approved_dataset_id")
    if dataset_id is None:
        return
    pipeline = _pipeline_repository()
    if pipeline is None:
        return
    try:
        preview = pipeline.invalidation_preview(int(dataset_id))
    except Exception as error:
        st.warning(f"Data history is temporarily unavailable: {error}")
        pipeline.close()
        return

    dataset = preview["dataset"]
    key = f"invalidate_dataset_{int(dataset_id)}"
    with st.expander("Data and result history", expanded=bool(st.session_state.get(key))):
        st.write(
            f"Current source: **{dataset['name']}**, version {dataset['version']} · "
            f"{dataset['start_date']:%b %-d, %Y}–{dataset['end_date']:%b %-d, %Y}"
        )
        st.caption(
            "If this source file was wrong, Credent can stop using it without deleting "
            "the historical record or manager-entered event notes."
        )
        if not st.session_state.get(key):
            if st.button(
                "Invalidate this dataset and all results derived from it",
                key=f"open_{key}",
                use_container_width=True,
            ):
                st.session_state[key] = True
                pipeline.close()
                st.rerun()
        else:
            st.warning(
                f"This will make {preview['runs']} model run(s), "
                f"{preview['forecasts']} forecast(s), {preview['events']} detected event(s), "
                "and their saved model outputs unavailable for normal use. "
                "Nothing will be permanently deleted."
            )
            if preview["queued_runs"] or preview["running_runs"]:
                st.info(
                    f"{preview['queued_runs']} queued job(s) will be canceled immediately. "
                    f"{preview['running_runs']} running job(s) will stop at a safe checkpoint."
                )
            reason = st.text_area(
                "Why is this data being invalidated?",
                key=f"reason_{key}",
                placeholder="Example: The wrong location's sales file was uploaded.",
            )
            confirmed = st.checkbox(
                "I understand that forecasts and detected results from this data will no longer be usable.",
                key=f"confirm_{key}",
            )
            confirm, keep = st.columns(2)
            if confirm.button(
                "Invalidate data and derived results",
                type="primary",
                disabled=not confirmed or not reason.strip(),
                key=f"confirm_button_{key}",
                use_container_width=True,
            ):
                try:
                    result = pipeline.invalidate_dataset(
                        int(dataset_id),
                        reason=reason,
                    )
                except Exception as error:
                    st.error(f"The dataset could not be invalidated: {error}")
                else:
                    dismissed = set(st.session_state.get("onboarding_dismissed_sites", ()))
                    dismissed.discard(int(site["site_id"]))
                    st.session_state.onboarding_dismissed_sites = tuple(sorted(dismissed))
                    st.session_state.pop("onboarding_context", None)
                    st.session_state.pop(key, None)
                    st.toast(
                        f"Dataset invalidated. {result['affected_run_count']} model run(s) were affected.",
                        icon="⚠️",
                    )
                    pipeline.close()
                    st.rerun()
            if keep.button(
                "Keep this data",
                key=f"cancel_{key}",
                use_container_width=True,
            ):
                st.session_state.pop(key, None)
                st.session_state.pop(f"reason_{key}", None)
                st.session_state.pop(f"confirm_{key}", None)
                pipeline.close()
                st.rerun()
    pipeline.close()


@st.fragment(run_every="2s")
def _render_forecast_job(run_id: int, review_id: int) -> None:
    """Render and automatically refresh one first-forecast job."""

    pipeline = _pipeline_repository()
    if pipeline is None:
        st.error("The local pipeline service is not installed in this environment.")
        return
    try:
        queue = pipeline.queue_status(run_id)
    except Exception as error:
        st.error(f"Job #{run_id} status is temporarily unavailable: {error}")
        return
    finally:
        pipeline.close()

    status = str(queue["status"])
    completed = int(queue.get("completed_steps") or 0)
    total = int(queue.get("total_steps") or 0)
    st.markdown(f"### Forecast job #{run_id}")
    if total:
        st.progress(
            min(completed / total, 1.0),
            text=f"{completed} of {total} pipeline stages complete",
        )

    if status == "queued":
        position = int(queue["position"] or 1)
        st.info(
            f"Job #{run_id} is queued at position #{position}. "
            f"Next: {_stage_label(queue.get('current_stage'))}."
        )
    elif status == "running":
        st.info(f"Job #{run_id} is running: {_stage_label(queue.get('current_stage'))}.")
    elif status == "failed":
        st.error(f"Job #{run_id} needs attention before it can be retried.")
        if st.button(
            "Retry first forecast",
            key=f"retry_first_forecast_{run_id}",
            type="primary",
            use_container_width=True,
        ):
            retry_pipeline = _pipeline_repository()
            repository = _repository()
            retry_run_id = None
            try:
                if retry_pipeline is None or repository is None:
                    raise RuntimeError("The local forecast services are unavailable.")
                controller = _workflow_controller(retry_pipeline)
                if controller is None:
                    raise RuntimeError("The workflow controller is unavailable.")
                retry_run_id, _ = controller.enqueue_active_first_forecast(review_id)
                repository.link_first_forecast(review_id, retry_run_id)
            except Exception as error:
                st.error(f"The first forecast could not be retried: {error}")
            finally:
                if retry_pipeline is not None:
                    retry_pipeline.close()
                if repository is not None:
                    repository.close()
            if retry_run_id is not None:
                st.rerun(scope="app")
    elif status == "published":
        st.success(f"Forecast job #{run_id} is complete. Opening your forecast…")
        st.session_state.completed_forecast_job = run_id
        st.rerun(scope="app")
    else:
        st.info(f"Job #{run_id}: {status.replace('_', ' ').title()}.")

    # Keep the detailed step table inside the same auto-refresh fragment as
    # the headline progress. Otherwise the summary advances every two seconds
    # while this table remains frozen at the app's initial render.
    render_run_details(run_id)


def _site_names(value: str) -> list[str]:
    return [name.strip() for name in value.splitlines() if name.strip()]


def _upload_widget_key(context: dict[str, Any]) -> str:
    mode = context.get("replace_review_id") or "initial"
    return f"sales_history_upload_{int(context['site_id'])}_{mode}"


def _organization_setup(repository) -> None:
    st.markdown('<div class="dashboard-kicker">Organization setup</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="dashboard-title">Welcome to Credent</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dashboard-subtitle">Name the organization and add all of the Sites or Units you want to begin setting up.</div>',
        unsafe_allow_html=True,
    )

    organizations = repository.organizations()
    if organizations:
        with st.expander("Continue an existing organization"):
            for organization in organizations:
                label = f"{organization['name']} · {organization['site_count']} site"
                if int(organization["site_count"]) != 1:
                    label += "s"
                if st.button(
                    label,
                    key=f"open_org_{organization['id']}",
                    use_container_width=True,
                ):
                    st.session_state.client_org_id = int(organization["id"])
                    st.session_state.pop("client_site_id", None)
                    st.rerun()

    st.markdown("### Start a new organization")
    organization_name = st.text_input("Organization name", placeholder="Example: Acme Hospitality")
    st.caption("Add several Sites/Units now. You can add more later.")
    edited_sites = st.text_area(
        "Sites / Units",
        placeholder="Downtown\nAirport\nNorthside",
        height=120,
        key="organization_site_editor",
        help="Enter one location, branch, department, or other independently forecasted unit per line.",
    )
    timezone = st.selectbox(
        "Default time zone",
        ["America/Denver", "America/Chicago", "America/New_York", "America/Los_Angeles", "UTC"],
    )
    if st.button("Create organization and Sites / Units", type="primary", use_container_width=True):
        names = _site_names(edited_sites)
        if not organization_name.strip():
            st.error("Enter an organization name.")
        elif not names:
            st.error("Add at least one Site or Unit.")
        else:
            try:
                created = repository.create_organization(
                    business_name=organization_name,
                    site_names=names,
                    timezone=timezone,
                )
            except ValueError as error:
                st.error(str(error).capitalize() + ".")
            else:
                st.session_state.client_org_id = int(created["org_id"])
                st.session_state.client_site_id = int(created["site_id"])
                st.rerun()


def render_client_shell() -> dict[str, Any] | None:
    """Render organization/site selection and return the selected site's state."""

    repository = _repository()
    if repository is None:
        st.error("The local onboarding service is not installed in this Streamlit environment.")
        return None
    try:
        org_id = st.session_state.get("client_org_id")
        if org_id is None:
            _organization_setup(repository)
            return None
        organizations = {int(item["id"]): item for item in repository.organizations()}
        organization = organizations.get(int(org_id))
        if organization is None:
            st.session_state.pop("client_org_id", None)
            st.session_state.pop("client_site_id", None)
            st.rerun()
        sites = repository.sites_for_org(int(org_id))
        if not sites:
            st.error("This organization has no Sites or Units yet.")
            return None
        sites_by_id = {int(item["site_id"]): item for item in sites}
        selected_id = int(st.session_state.get("client_site_id") or sites[0]["site_id"])
        if selected_id not in sites_by_id:
            selected_id = int(sites[0]["site_id"])
        labels = {int(item["site_id"]): str(item["site_name"]) for item in sites}
        with st.sidebar:
            st.markdown("### Organization")
            st.markdown(f"**{organization['name']}**")
            requested_id = st.selectbox(
                "Site / Unit",
                options=list(labels),
                index=list(labels).index(selected_id),
                format_func=labels.get,
                key="client_site_picker",
            )
            selected_id = int(requested_id)
            st.session_state.client_site_id = selected_id
            selected = sites_by_id[selected_id]
            st.caption(SITE_STATE_LABELS[selected["onboarding_state"]])
            with st.expander("Add Sites / Units"):
                additions = st.text_area(
                    "New Sites / Units",
                    placeholder="Westside\nWarehouse",
                    key="additional_site_editor",
                    help="Enter one Site or Unit per line.",
                )
                if st.button("Add to organization", use_container_width=True):
                    names = _site_names(additions)
                    if not names:
                        st.warning("Enter at least one Site or Unit.")
                    else:
                        try:
                            new_ids = repository.add_sites(
                                org_id=int(org_id),
                                site_names=names,
                                timezone=str(selected["timezone"]),
                                currency=str(selected["currency"]),
                            )
                        except ValueError as error:
                            st.error(str(error).capitalize() + ".")
                        else:
                            st.session_state.client_site_id = int(new_ids[0])
                            st.session_state.pop("client_site_picker", None)
                            st.session_state.pop("additional_site_editor", None)
                            st.rerun()
            monitoring_open = bool(st.session_state.get("show_job_monitoring"))
            if st.button(
                "Back to Site / Unit" if monitoring_open else "Jobs & workers",
                type="tertiary",
                use_container_width=True,
            ):
                st.session_state.show_job_monitoring = not monitoring_open
                st.rerun()
            if st.button("Switch organization", type="tertiary", use_container_width=True):
                st.session_state.pop("client_org_id", None)
                st.session_state.pop("client_site_id", None)
                st.session_state.pop("client_site_picker", None)
                st.session_state.pop("onboarding_context", None)
                st.rerun()
        selected = sites_by_id[selected_id]
        context = _site_context(selected)
        existing_context = st.session_state.get("onboarding_context") or {}
        if int(existing_context.get("site_id", -1)) != selected_id:
            st.session_state.onboarding_context = context
            st.session_state.pop("onboarding_force_site", None)
        else:
            existing_context.update(context)
            st.session_state.onboarding_context = existing_context
        return selected
    finally:
        repository.close()


def _progress(
    active: str,
    *,
    repository=None,
    context: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    labels = [
        ("upload", "Data"),
        ("schedule", "Schedule"),
        ("issues", "Review"),
        ("approval", "Approval"),
        ("diagnostics", "Diagnostics"),
        ("ready", "Ready"),
    ]
    order = [key for key, _ in labels]
    current = order.index(active) if active in order else 0
    columns = st.columns(len(labels), gap="small")
    editable_steps = {"upload", "schedule", "issues", "approval"}
    for index, ((step_key, label), column) in enumerate(zip(labels, columns)):
        progress_state = "done" if index < current else "active" if index == current else "future"
        can_go_back = (
            index < current
            and step_key in editable_steps
            and repository is not None
            and context is not None
            and state is not None
        )
        with column:
            if can_go_back:
                if st.button(
                    label,
                    key=f"onboard_nav_step{index + 1}_{state['id']}_{active}",
                    type="tertiary",
                    use_container_width=True,
                    help=f"Go back to {label}",
                ):
                    if step_key == "upload":
                        _replace_upload(context)
                    else:
                        review_id = int(state["id"])
                        if state.get("status") == "approved":
                            review_id = repository.revise_review(review_id)
                            context["review_id"] = review_id
                            st.session_state.onboarding_context = context
                        repository.set_step(review_id, step_key)
                        st.rerun()
            else:
                st.markdown(
                    f'<div class="onboard-step {progress_state}"><span>{index + 1}</span>{label}</div>',
                    unsafe_allow_html=True,
                )


def _read_upload(uploaded) -> pd.DataFrame:
    payload = uploaded.getvalue()
    suffix = Path(uploaded.name).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(BytesIO(payload))
    if suffix in {".xlsx", ".xlsm"}:
        return pd.read_excel(BytesIO(payload))
    raise ValueError("Please provide a CSV or Excel workbook.")


def _select_context(repository) -> dict[str, Any] | None:
    st.markdown("## Welcome to Credent")
    st.write(
        "We’ll review your first sales history with you, preserve the original upload, "
        "and prepare it for forecasting. Nothing will be forecast until you ask."
    )
    resumable = repository.resumable_customers()
    if resumable:
        st.markdown("### Continue where you left off")
        for item in resumable:
            label = f"{item['business_name']} · {item['site_name']}"
            detail = (
                f"Last step: {str(item.get('step') or 'welcome').replace('_', ' ').title()}"
                if item.get("review_id")
                else "Waiting for the first upload"
            )
            left, right = st.columns([4, 1])
            left.markdown(f"**{label}**  \n{detail}")
            if right.button("Resume", key=f"resume_onboarding_{item['org_id']}", use_container_width=True):
                context = {
                    "org_id": item["org_id"],
                    "site_id": item["site_id"],
                    "metric_id": item["metric_id"],
                    "review_id": item.get("review_id"),
                }
                st.session_state.onboarding_context = context
                st.rerun()
        st.divider()

    st.markdown("### Start a new setup")
    with st.form("new_customer_setup"):
        business_name = st.text_input("Business name")
        site_name = st.text_input("Primary location or operating unit")
        timezone = st.selectbox(
            "Time zone",
            ["America/Denver", "America/Chicago", "America/New_York", "America/Los_Angeles", "UTC"],
        )
        submitted = st.form_submit_button("Begin setup", type="primary", use_container_width=True)
    if submitted:
        if not business_name.strip() or not site_name.strip():
            st.error("Enter both a business name and a location or operating unit.")
        else:
            context = repository.create_customer(
                business_name=business_name,
                site_name=site_name,
                timezone=timezone,
            )
            context["review_id"] = None
            st.session_state.onboarding_context = context
            st.rerun()
    return None


def _upload_step(repository, context: dict[str, Any]) -> None:
    _progress("upload")
    st.markdown("## Give us your data")
    st.write("Upload daily history as CSV or Excel. Your original file will be retained unchanged.")
    st.info(
        "Your file must contain a **Date** column and a **Sales** column. "
        "Singular or plural names are accepted, including `Date`, `Dates`, `Sale`, and `Sales`. "
        "Other columns are ignored. After upload, confirm the Date and Sales values shown below."
    )
    replacing = context.get("replace_review_id") is not None
    if replacing:
        st.warning(
            "You’re replacing the current upload. The earlier file and decisions will remain in history."
        )
    upload_key = _upload_widget_key(context)
    uploaded = st.file_uploader(
        "Sales history",
        type=["csv", "xlsx", "xlsm"],
        key=upload_key,
    )
    if uploaded is None:
        return
    try:
        frame = _read_upload(uploaded)
    except Exception as error:
        st.error(f"We couldn’t read that file: {error}")
        return
    if frame.empty:
        st.error("The uploaded file has no rows.")
        return
    _, inspect_daily_data, parsed_daily_preview, select_daily_columns = _services()
    try:
        selected = select_daily_columns(frame)
        preview = parsed_daily_preview(frame)
        scan = inspect_daily_data(selected, date_col="Date", value_col="Sales")
    except Exception as error:
        st.error(
            f"We couldn’t identify a safe Date/Sales import: {error}. "
            "Rename the required columns and upload a new file."
        )
        return
    st.markdown("### Confirm what we found")
    st.caption("Only these two fields will be imported. Other uploaded columns are ignored.")
    st.dataframe(preview.head(20), width="stretch", hide_index=True)
    blocking = sum(issue.severity == "blocking" for issue in scan.issues)
    review = sum(issue.severity == "review" for issue in scan.issues)
    info = sum(issue.severity == "info" for issue in scan.issues)
    with st.container(key="upload_summary"):
        stats = st.columns([1, 1.15, 1, 1.65])
        stats[0].metric("Rows", f"{len(scan.rows):,}")
        stats[1].metric("Needs a decision", blocking + review)
        stats[2].metric("Missing notices", info)
        stats[3].metric(
            "Date range",
            f"{scan.rows['date'].min():%b %-d}–{scan.rows['date'].max():%b %-d}",
        )
    if st.button(
        "Save upload and review schedule",
        type="primary",
        use_container_width=True,
        key=f"save_upload_{int(context['site_id'])}_{context.get('replace_review_id') or 'initial'}",
    ):
        payload = uploaded.getvalue()
        stored = repository.store_original_upload(
            org_id=context["org_id"],
            site_id=context["site_id"],
            filename=uploaded.name,
            payload=payload,
        )
        review_id = repository.save_upload(
            org_id=context["org_id"],
            site_id=context["site_id"],
            metric_id=context["metric_id"],
            filename=uploaded.name,
            uri=stored.uri,
            source_hash=stored.digest,
            scan=scan,
            replace_review_id=context.get("replace_review_id"),
        )
        context["review_id"] = review_id
        context.pop("replace_review_id", None)
        st.session_state.onboarding_context = context
        st.session_state.pop(upload_key, None)
        st.rerun()


def _replace_upload(context: dict[str, Any]) -> None:
    context["replace_review_id"] = context.get("review_id")
    st.session_state.onboarding_context = context
    st.rerun()


def _schedule_step(repository, context: dict[str, Any], state: dict[str, Any]) -> None:
    _progress("schedule", repository=repository, context=context, state=state)
    st.markdown("## Does this look like your normal schedule?")
    st.write(
        "This is our best guess for future planning from the dates with reported activity. "
        "It is a proposal, not a permanent rule, and it does not decide whether historical sales are legitimate."
    )
    schedule_states: dict[int, str] = {}
    columns = st.columns(7)
    for day, column in zip(state["schedule"], columns):
        with column:
            confidence = float(day["confidence"] or 0)
            default = day["day_state"] == "open"
            is_open = st.toggle(
                DAY_NAMES[day["weekday"]][:3],
                value=default,
                key=f"schedule_{state['id']}_{day['weekday']}",
            )
            st.caption(f"{confidence:.0%} showed activity")
            schedule_states[int(day["weekday"])] = "open" if is_open else "closed"
    left, right = st.columns([1, 1])
    if left.button("Use a different file", use_container_width=True):
        _replace_upload(context)
    if right.button("Confirm schedule", type="primary", use_container_width=True):
        repository.save_schedule(state["id"], schedule_states)
        st.rerun()


def _review_rows(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for issue in issues:
        if issue["severity"] not in {"review", "blocking"}:
            continue
        key = (
            ("observation", int(issue["observation_id"]))
            if issue.get("observation_id") is not None
            else ("issue", int(issue["id"]))
        )
        row = grouped.setdefault(
            key,
            {
                "key": f"{key[0]}_{key[1]}",
                "issue_ids": [],
                "date": issue.get("date"),
                "observed": issue.get("observed"),
                "raw_value": issue.get("raw_value"),
                "issues": [],
                "messages": [],
                "entries": [],
            },
        )
        row["issue_ids"].append(int(issue["id"]))
        row["issues"].append(str(issue["issue_type"]).replace("_", " ").title())
        row["messages"].append(str(issue["message"]))
        row["entries"].append(
            {
                "id": int(issue["id"]),
                "issue_type": str(issue["issue_type"]),
                "decision": issue.get("decision"),
                "new_value": issue.get("new_value"),
            }
        )
    rows = []
    for row in grouped.values():
        active = {
            entry["decision"]
            for entry in row["entries"]
            if entry["decision"] not in {None, "defer"}
        }
        all_decided = all(
            entry["decision"] not in {None, "defer"} for entry in row["entries"]
        )
        row["decision"] = next(iter(active)) if all_decided and len(active) == 1 else None
        replacements = [
            entry["new_value"] for entry in row["entries"] if entry["new_value"] is not None
        ]
        row["new_value"] = replacements[-1] if replacements else None
        rows.append(row)
    return rows


def _entry_decision(entries: list[dict[str, Any]]) -> tuple[str | None, float | None]:
    """Return a shared decision only when every independently flagged issue is resolved."""

    if not entries or any(entry["decision"] in {None, "defer"} for entry in entries):
        return None, None
    decisions = {str(entry["decision"]) for entry in entries}
    decision = next(iter(decisions)) if len(decisions) == 1 else None
    replacements = [entry["new_value"] for entry in entries if entry["new_value"] is not None]
    return decision, replacements[-1] if replacements else None


def _issues_step(repository, context: dict[str, Any], state: dict[str, Any]) -> None:
    _progress("issues", repository=repository, context=context, state=state)
    st.markdown("## Review unusual data")
    st.write(
        "Unusual does not mean wrong. Keep legitimate values—including real events—or tell us how the model should treat them."
    )
    schedule_mismatches = [
        issue
        for issue in state["issues"]
        if issue["severity"] in {"review", "blocking"}
        and issue["issue_type"] in SCHEDULE_MISMATCH_TYPES
    ]
    accept_schedule_mismatches = False
    if schedule_mismatches:
        already_accepted = all(issue.get("decision") == "keep" for issue in schedule_mismatches)
        accept_schedule_mismatches = st.checkbox(
            "Sales that differ from the proposed schedule are acceptable.",
            value=already_accepted,
            key=f"accept_schedule_mismatches_{state['id']}",
            help=(
                "This accepts only historical schedule mismatches. Negative, nonnumeric, "
                "duplicate, and unusually high or low values still require their own review."
            ),
        )
        st.caption(
            "The proposed schedule is for future planning. It does not determine whether historical sales are legitimate."
        )
    rows = _review_rows(state["issues"])
    info = [issue for issue in state["issues"] if issue["severity"] == "info"]
    if info:
        with st.expander(f"{len(info)} missing-data notices · no decision required"):
            st.write(
                "These dates will remain missing. PyMC state-space can work with them, so we do not fill them automatically."
            )
            st.dataframe(
                pd.DataFrame(
                    [{"Date": issue["date"], "Notice": issue["message"]} for issue in info]
                ),
                hide_index=True,
                width="stretch",
            )
    if not rows:
        st.success("No values require a decision.")
    else:
        st.caption(
            "For duplicate dates, ignore the extra values until one remains. "
            "The final remaining value is selected automatically."
        )
        attempted = st.session_state.get("onboarding_review_attempt") == state["id"]
        duplicate_dates = set(st.session_state.get("onboarding_duplicate_dates", ()))
        selections = []
        for row in rows:
            actionable_entries = [
                entry
                for entry in row["entries"]
                if not (
                    accept_schedule_mismatches
                    and entry["issue_type"] in SCHEDULE_MISMATCH_TYPES
                )
            ]
            current_decision, current_new_value = _entry_decision(actionable_entries)
            unresolved = bool(actionable_entries) and current_decision is None
            with st.container(border=True, key=f"review_row_{state['id']}_{row['key']}"):
                if attempted and (unresolved or row["date"] in duplicate_dates):
                    if row["date"] in duplicate_dates:
                        st.error("This duplicate date must have exactly one value left to model.")
                    else:
                        st.error("Choose how this row should be handled before continuing.")
                summary, control = st.columns([3, 2], gap="large")
                with summary:
                    date_label = row["date"].strftime("%B %-d, %Y") if row["date"] else "Invalid date"
                    value = row["observed"]
                    value_label = f"${float(value):,.2f}" if value is not None else str(row["raw_value"] or "Blank")
                    st.markdown(f"**{date_label} · {value_label}**")
                    st.caption(" · ".join(dict.fromkeys(row["issues"])))
                    for message in dict.fromkeys(row["messages"]):
                        st.caption(message)
                default_label = DECISION_CODES.get(current_decision, "Choose…")
                with control:
                    replacement = None
                    if not actionable_entries:
                        st.success("Accepted by the proposed-schedule option above.")
                        decision = None
                    else:
                        decision_label = st.selectbox(
                            "Decision",
                            options=list(DECISION_LABELS),
                            index=list(DECISION_LABELS).index(default_label),
                            key=f"review_decision_{state['id']}_{row['key']}",
                        )
                        decision = DECISION_LABELS[decision_label]
                        if decision == "replace":
                            replacement = st.number_input(
                                "New value",
                                value=float(current_new_value or 0),
                                step=1.0,
                                key=f"review_value_{state['id']}_{row['key']}",
                            )
                selections.append((row, actionable_entries, decision, replacement))
        if st.button("Save decisions and continue", type="primary", use_container_width=True):
            errors = []
            if accept_schedule_mismatches:
                for issue in schedule_mismatches:
                    repository.save_action(
                        int(issue["id"]),
                        decision="keep",
                        reason="Accepted as a historical mismatch with the proposed planning schedule.",
                    )
            for row, actionable_entries, decision, replacement in selections:
                if not actionable_entries:
                    continue
                if decision is None:
                    errors.append(row["key"])
                    continue
                repository.save_row_action(
                    [int(entry["id"]) for entry in actionable_entries],
                    decision=decision,
                    new_value=float(replacement) if decision == "replace" else None,
                )
            unresolved = repository.unresolved_count(state["id"])
            duplicate_errors = repository.duplicate_group_errors(state["id"])
            if unresolved or duplicate_errors:
                st.session_state.onboarding_review_attempt = state["id"]
                st.session_state.onboarding_duplicate_dates = tuple(
                    item["date"] for item in duplicate_errors
                )
            else:
                st.session_state.pop("onboarding_review_attempt", None)
                st.session_state.pop("onboarding_duplicate_dates", None)
                for row, _, _, _ in selections:
                    st.session_state.pop(f"review_decision_{state['id']}_{row['key']}", None)
                    st.session_state.pop(f"review_value_{state['id']}_{row['key']}", None)
                repository.set_step(state["id"], "approval")
            st.rerun()
        unresolved = repository.unresolved_count(state["id"])
        if unresolved:
            st.info(f"{unresolved} issue{'s' if unresolved != 1 else ''} still need a decision.")
    if not rows and st.button("Continue to approval", type="primary", use_container_width=True):
        repository.set_step(state["id"], "approval")
        st.rerun()
    back, replace = st.columns(2)
    if back.button("Back to schedule", use_container_width=True):
        repository.set_step(state["id"], "schedule")
        st.rerun()
    if replace.button("Use a different file", use_container_width=True):
        _replace_upload(context)


def _approval_step(repository, context: dict[str, Any], state: dict[str, Any]) -> None:
    _progress("approval", repository=repository, context=context, state=state)
    st.markdown("## Approve the reviewed data")
    reviewed_rows = _review_rows(state["issues"])
    counts = pd.Series([row["decision"] for row in reviewed_rows if row["decision"]]).value_counts()
    columns = st.columns(5)
    columns[0].metric("Kept", int(counts.get("keep", 0)))
    columns[1].metric("Ignored", int(counts.get("ignore", 0)))
    columns[2].metric("Replaced", int(counts.get("replace", 0)))
    columns[3].metric("Left missing", int(counts.get("missing", 0)))
    columns[4].metric("Marked closed", int(counts.get("closed", 0)))
    st.info(
        "Approval preserves the original upload and records these decisions as a new review version. It does not create a forecast."
    )
    confirmed = st.checkbox(
        "I’ve reviewed the proposed schedule and flagged values.",
        key=f"approval_confirmed_{state['id']}",
    )
    approve = st.button(
        "Approve data and run diagnostics",
        type="primary" if confirmed else "secondary",
        use_container_width=True,
        disabled=not confirmed,
        key=f"approve_data_{state['id']}",
    )
    if approve:
        with st.spinner("Preparing the approved panel and checking modeling readiness…"):
            repository.approve_and_diagnose(state["id"])
        st.rerun()
    if st.button("Back to data decisions", use_container_width=True):
        repository.set_step(state["id"], "issues")
        st.rerun()


def _ready_step(repository, context: dict[str, Any], state: dict[str, Any]) -> None:
    _progress("ready", repository=repository, context=context, state=state)
    st.markdown("## Your data is ready")
    diagnostic = dict(state.get("diagnostic") or {})
    columns = st.columns(4)
    columns[0].metric("Rows", f"{int(diagnostic.get('rows', 0)):,}")
    columns[1].metric("Observed", f"{int(diagnostic.get('observed_rows', 0)):,}")
    columns[2].metric("Missing", f"{float(diagnostic.get('missing_rate', 0)):.1%}")
    level = diagnostic.get("initial_level")
    columns[3].metric("Typical level", f"${level:,.0f}" if level is not None else "—")
    st.success(
        "The upload, operating schedule, review decisions, and provisional diagnostics are saved."
    )
    st.caption(
        "These diagnostics are scaffolding for the Forecast 1 prior-calibration adapter; no model has been fitted yet."
    )
    st.info("Setup is complete. Forecasting starts from this Site / Unit’s page.")
    if st.button(
        "Go to your Site / Unit page to start forecasting",
        type="primary",
        use_container_width=True,
    ):
        _leave_wizard(int(context["site_id"]))
        st.rerun()


def _leave_wizard(site_id: int) -> None:
    dismissed = set(st.session_state.get("onboarding_dismissed_sites", ()))
    dismissed.add(int(site_id))
    st.session_state.onboarding_dismissed_sites = tuple(sorted(dismissed))
    st.session_state.pop("onboarding_force_site", None)
    context = st.session_state.get("onboarding_context") or {}
    if int(context.get("site_id", -1)) == int(site_id):
        st.session_state.pop(_upload_widget_key(context), None)
        context.pop("replace_review_id", None)
        st.session_state.onboarding_context = context


def _discard_changes_and_leave(repository, context: dict[str, Any]) -> None:
    review_id = context.get("review_id")
    approved_review_id = context.get("approved_review_id")
    if review_id is not None and int(review_id) != int(approved_review_id or -1):
        repository.discard_review(int(review_id))
    _leave_wizard(int(context["site_id"]))


def render_site_home(site: dict[str, Any]) -> None:
    """Render the intentionally sparse site home shown before a first forecast exists."""

    repository = _repository()
    if repository is None:
        st.error("The local onboarding service is not installed in this Streamlit environment.")
        return
    context = _site_context(site)
    state = None
    try:
        home_review_id = site.get("approved_review_id") or context.get("review_id")
        if home_review_id is not None:
            state = repository.review_state(int(home_review_id))
        st.markdown(f'<div class="dashboard-kicker">{site["site_name"]}</div>', unsafe_allow_html=True)
        st.markdown('<h1 class="dashboard-title">Site home</h1>', unsafe_allow_html=True)
        onboarding_state = str(site["onboarding_state"])
        if onboarding_state in {"needs_data", "reviewing"}:
            st.markdown(
                '<div class="dashboard-subtitle">Finish reviewing this Site / Unit’s data before requesting its first forecast.</div>',
                unsafe_allow_html=True,
            )
            if st.button("Continue data setup", type="primary", use_container_width=True):
                dismissed = set(st.session_state.get("onboarding_dismissed_sites", ()))
                dismissed.discard(int(site["site_id"]))
                st.session_state.onboarding_dismissed_sites = tuple(sorted(dismissed))
                st.session_state.onboarding_context = context
                st.session_state.onboarding_force_site = int(site["site_id"])
                st.rerun()
            return

        diagnostic = dict((state or {}).get("diagnostic") or {})
        if diagnostic:
            columns = st.columns(3)
            columns[0].metric("Imported days", f"{int(diagnostic.get('rows', 0)):,}")
            columns[1].metric("Observed days", f"{int(diagnostic.get('observed_rows', 0)):,}")
            columns[2].metric("Missing", f"{float(diagnostic.get('missing_rate', 0)):.1%}")

        if onboarding_state == "ready_for_forecast":
            st.success("The data review and initial diagnostics are complete.")
            st.markdown("## Get your first forecast")
            st.write("This starts a real queued onboarding run for this Site / Unit.")
            if st.button("Start your first forecast", type="primary", use_container_width=True):
                pipeline = _pipeline_repository()
                if pipeline is None:
                    st.error("The local pipeline service is not installed in this environment.")
                else:
                    queued = False
                    try:
                        controller = _workflow_controller(pipeline)
                        if controller is None:
                            raise RuntimeError("The workflow controller is unavailable.")
                        run_id, _ = controller.enqueue_active_first_forecast(
                            int(state["id"])
                        )
                        repository.link_first_forecast(int(state["id"]), run_id)
                        queued = True
                    except Exception as error:
                        st.error(f"The first forecast could not be queued: {error}")
                    finally:
                        pipeline.close()
                    if queued:
                        st.rerun()
        elif onboarding_state == "forecasting":
            st.markdown("## Your first forecast is in progress")
            if site.get("forecast_run_id") is None:
                st.info("The forecast request is recorded and waiting for the local worker.")
            else:
                _render_forecast_job(
                    int(site["forecast_run_id"]),
                    int(state["id"]),
                )

        if state is not None and onboarding_state == "ready_for_forecast":
            st.divider()
            left, right = st.columns(2)
            if left.button("Review or change data decisions", use_container_width=True):
                draft_review_id = site.get("draft_review_id")
                context["review_id"] = (
                    int(draft_review_id)
                    if draft_review_id is not None
                    else repository.revise_review(int(state["id"]))
                )
                dismissed = set(st.session_state.get("onboarding_dismissed_sites", ()))
                dismissed.discard(int(site["site_id"]))
                st.session_state.onboarding_dismissed_sites = tuple(sorted(dismissed))
                st.session_state.onboarding_context = context
                st.session_state.onboarding_force_site = int(site["site_id"])
                st.rerun()
            if right.button("Use a different dataset", use_container_width=True):
                context["replace_review_id"] = int(state["id"])
                st.session_state.onboarding_context = context
                st.session_state.onboarding_force_site = int(site["site_id"])
                st.rerun()
        render_data_validity_controls(site)
    finally:
        repository.close()


def render_onboarding_workspace(
    context: dict[str, Any] | None = None,
    *,
    show_exit: bool = True,
) -> None:
    st.markdown(
        """
        <style>
        .onboard-progress { display:flex; gap:.5rem; margin:.4rem 0 1.6rem; overflow-x:auto; }
        .onboard-step { min-width:95px; flex:1; color:#89918c; font-size:.76rem; font-weight:650; }
        .onboard-step span { display:inline-grid; place-items:center; width:1.5rem; height:1.5rem;
            margin-right:.35rem; border-radius:999px; background:#e6e3da; color:#66716a; }
        .onboard-step.done { color:#527866; }
        .onboard-step.done span { background:#bcd4c7; color:#214f3d; }
        .onboard-step.active { color:#1d2a24; }
        .onboard-step.active span { background:#2f6b55; color:white; }
        [class*="st-key-onboard_nav_step"] button {
            min-height: 1.75rem;
            padding: 0;
            justify-content: flex-start;
            color: #527866;
            font-size: .76rem;
            font-weight: 650;
        }
        [class*="st-key-onboard_nav_step"] button::before {
            display: inline-grid;
            place-items: center;
            width: 1.5rem;
            height: 1.5rem;
            flex: 0 0 1.5rem;
            margin-right: .35rem;
            border-radius: 999px;
            background: #bcd4c7;
            color: #214f3d;
            font-size: .76rem;
            line-height: 1;
        }
        [class*="st-key-onboard_nav_step1_"] button::before { content: "1"; }
        [class*="st-key-onboard_nav_step2_"] button::before { content: "2"; }
        [class*="st-key-onboard_nav_step3_"] button::before { content: "3"; }
        [class*="st-key-onboard_nav_step4_"] button::before { content: "4"; }
        .st-key-upload_summary [data-testid="stMetric"] {
            height: 110px;
            box-sizing: border-box;
        }
        .st-key-upload_summary [data-testid="stColumn"]:last-child [data-testid="stMetricValue"] {
            font-size: clamp(1.4rem, 2.4vw, 2rem);
            white-space: nowrap;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    repository = _repository()
    if repository is None:
        st.error("The local onboarding service is not installed in this Streamlit environment.")
        return
    try:
        context = context or st.session_state.get("onboarding_context")
        if context is None:
            _select_context(repository)
            return
        review_id = context.get("review_id")
        if review_id is None or context.get("replace_review_id") is not None:
            _upload_step(repository, context)
            if show_exit and context.get("first_import"):
                if st.button(
                    "Discard changes and return to Site / Unit home",
                    type="tertiary",
                    key="cancel_replacement_upload",
                ):
                    _discard_changes_and_leave(repository, context)
                    st.rerun()
            return
        state = repository.review_state(review_id)
        step = state["step"]
        if step == "schedule":
            _schedule_step(repository, context, state)
        elif step == "issues":
            _issues_step(repository, context, state)
        elif step == "approval":
            _approval_step(repository, context, state)
        elif step in {"diagnostics", "ready"}:
            _ready_step(repository, context, state)
        else:
            _upload_step(repository, context)
        if show_exit and context.get("first_import") and step != "ready":
            if st.button(
                "Discard changes and return to Site / Unit home",
                type="tertiary",
                key=f"discard_review_{state['id']}",
            ):
                _discard_changes_and_leave(repository, context)
                st.rerun()
    except Exception as error:
        st.error(f"The onboarding workspace couldn’t continue: {error}")
    finally:
        repository.close()
