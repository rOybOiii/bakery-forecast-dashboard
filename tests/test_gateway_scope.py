from __future__ import annotations

from types import SimpleNamespace

import gateway_client


def test_gateway_scope_survives_a_fragment_context_reset(monkeypatch) -> None:
    session_state: dict[str, object] = {}
    monkeypatch.setattr(
        gateway_client,
        "_store_session_scope",
        lambda scope: session_state.__setitem__(
            gateway_client._SESSION_SCOPE_KEY,
            {
                "base_url": scope["client"].base_url,
                "token": scope["client"].token,
                "org_id": scope["org_id"],
                "site_id": scope["site_id"],
                "metric_id": scope["metric_id"],
            },
        ),
    )
    monkeypatch.setattr(
        gateway_client,
        "st",
        SimpleNamespace(session_state=session_state),
        raising=False,
    )

    identity = SimpleNamespace(
        gateway_url="https://api.credib.ly",
        gateway_token="signed-access-token",
    )
    gateway_client.bind_gateway_scope(
        identity,
        org_id=11,
        site_id=22,
        metric_id=33,
    )
    gateway_client._ACTIVE_SCOPE.set(None)

    monkeypatch.setitem(
        __import__("sys").modules,
        "streamlit",
        SimpleNamespace(session_state=session_state),
    )
    restored = gateway_client.active_gateway_scope()

    assert restored is not None
    assert restored["client"].base_url == "https://api.credib.ly"
    assert restored["client"].token == "signed-access-token"
    assert restored["org_id"] == 11
    assert restored["site_id"] == 22
    assert restored["metric_id"] == 33


def test_gateway_client_cancels_one_forecast_job(monkeypatch) -> None:
    client = gateway_client.GatewayClient(
        "https://api.credib.ly", "signed-access-token"
    )
    requests: list[tuple[str, str]] = []

    def request(method: str, path: str, **_kwargs):
        requests.append((method, path))
        return {"status": "cancellation_requested"}

    monkeypatch.setattr(client, "_request", request)

    assert client.cancel_job(7, 41) == "cancellation_requested"
    assert requests == [("POST", "/v1/organizations/7/jobs/41/cancel")]


def test_gateway_client_reruns_first_forecast_with_current_data(monkeypatch) -> None:
    client = gateway_client.GatewayClient(
        "https://api.credib.ly", "signed-access-token"
    )
    requests: list[tuple[str, str]] = []

    def request(method: str, path: str, **_kwargs):
        requests.append((method, path))
        return {"run_id": 52, "created": True}

    monkeypatch.setattr(client, "_request", request)

    assert client.rerun_first_forecast(7, 8, 9) == (52, True)
    assert requests == [
        (
            "POST",
            "/v1/organizations/7/sites/8/metrics/9/forecasts/rerun",
        )
    ]


def test_gateway_client_records_site_onboarding_skip(monkeypatch) -> None:
    client = gateway_client.GatewayClient(
        "https://api.credib.ly", "signed-access-token"
    )
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request(method: str, path: str, **kwargs):
        requests.append((method, path, kwargs.get("json")))
        return None

    monkeypatch.setattr(client, "_request", request)

    client.set_site_onboarding_skipped(7, 8, skipped=True)

    assert requests == [
        (
            "PUT",
            "/v1/organizations/7/sites/8/onboarding-skip",
            {"skipped": True},
        )
    ]
