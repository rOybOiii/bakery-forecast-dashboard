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
