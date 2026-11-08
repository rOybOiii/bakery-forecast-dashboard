from __future__ import annotations

import onboarding_workspace


class _Pipeline:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Repository:
    def __init__(self) -> None:
        self.links: list[tuple[int, int]] = []
        self.closed = False

    def link_first_forecast_rerun(self, review_id: int, run_id: int) -> None:
        self.links.append((review_id, run_id))

    def close(self) -> None:
        self.closed = True


class _Controller:
    def __init__(self) -> None:
        self.review_ids: list[int] = []

    def enqueue_active_first_forecast(self, review_id: int) -> tuple[int, bool]:
        self.review_ids.append(review_id)
        return 42, False


def test_enqueue_fresh_first_forecast_uses_active_recipe_and_relinks_review(
    monkeypatch,
) -> None:
    pipeline = _Pipeline()
    repository = _Repository()
    controller = _Controller()
    monkeypatch.setattr(onboarding_workspace, "_pipeline_repository", lambda: pipeline)
    monkeypatch.setattr(onboarding_workspace, "_repository", lambda: repository)
    monkeypatch.setattr(
        onboarding_workspace,
        "_workflow_controller",
        lambda value: controller if value is pipeline else None,
    )

    run_id = onboarding_workspace._enqueue_fresh_first_forecast(7)

    assert run_id == 42
    assert controller.review_ids == [7]
    assert repository.links == [(7, 42)]
    assert pipeline.closed
    assert repository.closed


def test_organization_onboarding_requires_every_site_to_be_reviewed() -> None:
    summary = onboarding_workspace.organization_onboarding_summary(
        [
            {"site_id": 1, "onboarding_state": "ready_for_forecast"},
            {"site_id": 2, "onboarding_state": "skipped"},
            {"site_id": 3, "onboarding_state": "needs_data"},
        ]
    )

    assert summary["reviewed_count"] == 2
    assert not summary["all_reviewed"]
    assert not summary["can_forecast"]
    assert [site["site_id"] for site in summary["unresolved"]] == [3]


def test_organization_onboarding_needs_one_data_ready_site() -> None:
    all_skipped = onboarding_workspace.organization_onboarding_summary(
        [
            {"site_id": 1, "onboarding_state": "skipped"},
            {"site_id": 2, "onboarding_state": "skipped"},
        ]
    )
    one_ready = onboarding_workspace.organization_onboarding_summary(
        [
            {"site_id": 1, "onboarding_state": "ready_for_forecast"},
            {"site_id": 2, "onboarding_state": "skipped"},
        ]
    )

    assert all_skipped["all_reviewed"]
    assert not all_skipped["can_forecast"]
    assert one_ready["all_reviewed"]
    assert one_ready["can_forecast"]


def test_established_organization_is_not_relocked_by_a_new_site() -> None:
    sites = [
        {"site_id": 1, "onboarding_state": "active"},
        {"site_id": 2, "onboarding_state": "needs_data"},
    ]

    assert not onboarding_workspace.organization_requires_onboarding_gate(
        sites,
        force_site_id=None,
    )
    assert not onboarding_workspace.organization_requires_onboarding_gate(
        [{"site_id": 2, "onboarding_state": "needs_data"}],
        force_site_id=2,
    )
