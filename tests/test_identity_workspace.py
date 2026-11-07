from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from gateway_client import GatewayError
from identity_workspace import _gateway_access_token


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_auth_example_requests_credibly_api_access_token() -> None:
    with (PROJECT_ROOT / ".streamlit" / "secrets.toml.example").open("rb") as file:
        auth = tomllib.load(file)["auth"]

    assert auth["expose_tokens"] == "access"
    assert auth["server_metadata_url"] == (
        "https://login.credib.ly/.well-known/openid-configuration"
    )
    assert auth["client_kwargs"]["audience"] == "https://api.credib.ly"
    assert auth["client_kwargs"]["scope"] == "openid profile email"


def test_gateway_access_token_uses_access_token_not_id_token() -> None:
    token = _gateway_access_token(
        {
            "id": "identity-token-must-not-reach-the-api",
            "access": "  api-access-token  ",
        }
    )

    assert token == "api-access-token"


def test_gateway_access_token_rejects_id_only_login() -> None:
    with pytest.raises(GatewayError, match="API access token"):
        _gateway_access_token({"id": "identity-token"})


@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_gateway_access_token_rejects_invalid_values(value: object) -> None:
    with pytest.raises(GatewayError, match="invalid API access token"):
        _gateway_access_token({"access": value})
