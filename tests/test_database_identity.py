from __future__ import annotations

import pytest

from database_identity import normalized_email, oidc_identity


def test_oidc_identity_requires_verified_email_and_namespaces_subject() -> None:
    identity = oidc_identity(
        {
            "iss": "https://accounts.example.test",
            "sub": "customer-42",
            "email": " Manager@Example.COM ",
            "email_verified": True,
            "name": "Site Manager",
        }
    )

    assert identity == {
        "auth_id": "https://accounts.example.test|customer-42",
        "email": "manager@example.com",
        "name": "Site Manager",
    }


def test_oidc_identity_rejects_unverified_email() -> None:
    with pytest.raises(ValueError, match="verify your email"):
        oidc_identity(
            {
                "iss": "https://accounts.example.test",
                "sub": "customer-42",
                "email": "manager@example.com",
                "email_verified": False,
            }
        )


@pytest.mark.parametrize(
    "value",
    ["missing-at-sign", "missing.example.com", "name@", "two words@example.com"],
)
def test_normalized_email_rejects_invalid_addresses(value: str) -> None:
    with pytest.raises(ValueError, match="valid email"):
        normalized_email(value)
