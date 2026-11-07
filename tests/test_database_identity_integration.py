from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from database_identity import IdentityRepository


def test_membership_administration_and_channels_are_org_scoped() -> None:
    if os.environ.get("CREDENT_RUN_DB_TESTS") != "1":
        pytest.skip("set CREDENT_RUN_DB_TESTS=1 to run PostgreSQL integration tests")
    repository = IdentityRepository()
    suffix = uuid4().hex
    org_id = None
    invited_account_id = None
    try:
        owner = repository.local_identity()
        with repository.engine.begin() as connection:
            org_id = connection.execute(
                text(
                    """
                    INSERT INTO org (name, slug)
                    VALUES (:name, :slug) RETURNING id
                    """
                ),
                {"name": f"Identity test {suffix}", "slug": f"identity-{suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO member (org_id, account_id, role, status)
                    VALUES (:org_id, :account_id, 'org_admin', 'active')
                    """
                ),
                {"org_id": org_id, "account_id": owner.account_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO category (org_id, name, system_key)
                    VALUES (:org_id, 'Unidentified', 'unidentified')
                    """
                ),
                {"org_id": org_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO metric (org_id, name, kind, unit, grain, currency)
                    VALUES (:org_id, 'Sales', 'sales', 'dollars', 'day', 'USD')
                    """
                ),
                {"org_id": org_id},
            )
            site_id = connection.execute(
                text(
                    """
                    INSERT INTO site (
                        org_id, name, code, kind, timezone, currency, first_import
                    ) VALUES (
                        :org_id, 'Original site', 'ORIGINAL', 'business',
                        'America/Denver', 'USD', false
                    ) RETURNING id
                    """
                ),
                {"org_id": org_id},
            ).scalar_one()

        email = f"admin-{suffix}@example.test"
        assert repository.invite_member(owner, org_id, email) == "invited"
        invited = repository.resolve_verified_identity(
            auth_id=f"https://identity.example.test|{suffix}",
            email=email,
            name="Invited Admin",
        )
        invited_account_id = invited.account_id
        repository.rename_organization(owner, org_id, "Renamed organization")
        repository.rename_site(owner, org_id, site_id, "Renamed site")
        repository.set_site_channels(
            invited,
            org_id,
            site_id,
            {"Search", "Email"},
        )

        workspace = repository.organization_settings(invited, org_id)
        assert workspace["organization"]["name"] == "Renamed organization"
        assert workspace["sites"][0]["name"] == "Renamed site"
        assert workspace["enabled_channels"][site_id] == {"Search", "Email"}
        assert {member["email"] for member in workspace["members"]} >= {
            owner.email,
            invited.email,
        }
        assert workspace["invitations"] == []

        owner_member = next(
            member for member in workspace["members"] if member["account_id"] == owner.account_id
        )
        invited_member = next(
            member
            for member in workspace["members"]
            if member["account_id"] == invited.account_id
        )
        repository.set_member_status(
            owner,
            org_id,
            int(invited_member["member_id"]),
            "revoked",
        )
        with pytest.raises(ValueError, match="at least one active"):
            repository.set_member_status(
                owner,
                org_id,
                int(owner_member["member_id"]),
                "revoked",
            )
    finally:
        if org_id is not None:
            with repository.engine.begin() as connection:
                connection.execute(text("DELETE FROM audit WHERE org_id = :id"), {"id": org_id})
                connection.execute(
                    text(
                        "DELETE FROM channel_policy WHERE site_id IN "
                        "(SELECT id FROM site WHERE org_id = :id)"
                    ),
                    {"id": org_id},
                )
                connection.execute(text("DELETE FROM channel WHERE org_id = :id"), {"id": org_id})
                connection.execute(
                    text("DELETE FROM org_invitation WHERE org_id = :id"), {"id": org_id}
                )
                connection.execute(text("DELETE FROM member WHERE org_id = :id"), {"id": org_id})
                connection.execute(text("DELETE FROM metric WHERE org_id = :id"), {"id": org_id})
                connection.execute(text("DELETE FROM site WHERE org_id = :id"), {"id": org_id})
                connection.execute(text("DELETE FROM category WHERE org_id = :id"), {"id": org_id})
                connection.execute(text("DELETE FROM org WHERE id = :id"), {"id": org_id})
                if invited_account_id is not None:
                    connection.execute(
                        text("DELETE FROM account WHERE id = :id"),
                        {"id": invited_account_id},
                    )
        repository.close()
