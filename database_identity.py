"""Verified identity, organization authorization, and MVP administration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from sqlalchemy import Engine, text


ROOT = Path(__file__).resolve().parent
CORE_DATABASE = ROOT.parent / "Credent Core" / "packages" / "database" / "src"
if CORE_DATABASE.exists() and str(CORE_DATABASE) not in sys.path:
    sys.path.insert(0, str(CORE_DATABASE))

from credent_db import make_engine  # noqa: E402


MVP_CHANNELS = (
    "Search",
    "Social",
    "Display",
    "Email",
    "Direct mail",
    "Radio",
    "Television",
    "Outdoor",
)


@dataclass(frozen=True)
class Identity:
    account_id: int
    auth_id: str
    name: str
    email: str
    system_role: str
    gateway_token: str | None = None
    gateway_url: str | None = None

    @property
    def is_system_admin(self) -> bool:
        return self.system_role == "system_admin"

    @property
    def actor_role(self) -> str:
        return "system_admin" if self.is_system_admin else "org_admin"


def normalized_email(value: object) -> str:
    email = str(value or "").strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("enter a valid email address")
    return email


def oidc_identity(claims: Mapping[str, Any]) -> dict[str, str]:
    """Extract the stable, verified identity fields Credent accepts from OIDC."""

    subject = str(claims.get("sub") or "").strip()
    issuer = str(claims.get("iss") or "oidc").strip()
    if not subject:
        raise ValueError("the identity provider did not return a stable user ID")
    verified = claims.get("email_verified")
    if verified not in {True, "true", "True", 1, "1"}:
        raise ValueError("verify your email address with the identity provider first")
    email = normalized_email(claims.get("email"))
    name = str(claims.get("name") or claims.get("preferred_username") or email).strip()
    return {
        "auth_id": f"{issuer}|{subject}",
        "email": email,
        "name": name or email,
    }


class IdentityRepository:
    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or make_engine()
        self._owns_engine = engine is None

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    @staticmethod
    def _identity(row: Mapping[str, Any]) -> Identity:
        if row["status"] != "active":
            raise PermissionError("this account is disabled")
        return Identity(
            account_id=int(row["id"]),
            auth_id=str(row["auth_id"]),
            name=str(row["name"]),
            email=str(row["email"]),
            system_role=str(row["system_role"]),
        )

    @staticmethod
    def _audit(
        connection,
        *,
        identity: Identity,
        org_id: int,
        action: str,
        object_type: str,
        object_id: object,
        change_set: dict[str, Any] | None = None,
        site_id: int | None = None,
        reason: str | None = None,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO audit (
                    org_id, site_id, actor_id, actor_name, actor_role,
                    action, object_type, object_id, change_set, reason, source
                ) VALUES (
                    :org_id, :site_id, :actor_id, :actor_name, :actor_role,
                    :action, :object_type, :object_id, CAST(:change_set AS jsonb),
                    :reason, 'manager_dashboard'
                )
                """
            ),
            {
                "org_id": org_id,
                "site_id": site_id,
                "actor_id": identity.account_id,
                "actor_name": identity.name,
                "actor_role": identity.actor_role,
                "action": action,
                "object_type": object_type,
                "object_id": str(object_id),
                "change_set": __import__("json").dumps(change_set or {}),
                "reason": reason,
            },
        )

    def local_identity(self, auth_id: str = "local-streamlit-manager") -> Identity:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    UPDATE account SET last_login_at = now()
                    WHERE auth_id = :auth_id
                    RETURNING id, auth_id, name, email, status, system_role
                    """
                ),
                {"auth_id": auth_id},
            ).mappings().one_or_none()
        if row is None:
            raise PermissionError("the local development identity is not configured")
        return self._identity(row)

    def resolve_verified_identity(
        self,
        *,
        auth_id: str,
        email: str,
        name: str,
    ) -> Identity:
        """Synchronize one verified provider identity and accept matching invitations."""

        email = normalized_email(email)
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            row = connection.execute(
                text("SELECT * FROM account WHERE auth_id = :auth_id FOR UPDATE"),
                {"auth_id": auth_id},
            ).mappings().one_or_none()
            if row is None:
                collision = connection.execute(
                    text("SELECT auth_id FROM account WHERE lower(email) = :email"),
                    {"email": email},
                ).scalar_one_or_none()
                if collision is not None:
                    raise PermissionError(
                        "that verified email is already attached to another sign-in identity"
                    )
                row = connection.execute(
                    text(
                        """
                        INSERT INTO account (
                            auth_id, name, email, status, system_role,
                            email_verified_at, last_login_at
                        ) VALUES (
                            :auth_id, :name, :email, 'active', 'customer', :now, :now
                        )
                        RETURNING *
                        """
                    ),
                    {"auth_id": auth_id, "name": name, "email": email, "now": now},
                ).mappings().one()
            else:
                row = connection.execute(
                    text(
                        """
                        UPDATE account
                        SET name = :name, email = :email,
                            email_verified_at = COALESCE(email_verified_at, :now),
                            last_login_at = :now
                        WHERE id = :id
                        RETURNING *
                        """
                    ),
                    {"id": row["id"], "name": name, "email": email, "now": now},
                ).mappings().one()
            identity = self._identity(row)
            invitations = connection.execute(
                text(
                    """
                    SELECT * FROM org_invitation
                    WHERE lower(email) = :email AND status = 'pending'
                      AND (expires_at IS NULL OR expires_at > now())
                    FOR UPDATE
                    """
                ),
                {"email": email},
            ).mappings().all()
            for invitation in invitations:
                connection.execute(
                    text(
                        """
                        INSERT INTO member (org_id, account_id, role, status)
                        VALUES (:org_id, :account_id, 'org_admin', 'active')
                        ON CONFLICT (org_id, account_id) DO UPDATE
                        SET role = 'org_admin', status = 'active'
                        """
                    ),
                    {"org_id": invitation["org_id"], "account_id": identity.account_id},
                )
                connection.execute(
                    text(
                        """
                        UPDATE org_invitation
                        SET status = 'accepted', accepted_by = :account_id,
                            accepted_at = now()
                        WHERE id = :id
                        """
                    ),
                    {"id": invitation["id"], "account_id": identity.account_id},
                )
        return identity

    def organizations_for(self, identity: Identity) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT o.id, o.name, COUNT(DISTINCT s.id) AS site_count
                    FROM org o
                    LEFT JOIN site s ON s.org_id = o.id AND s.status = 'active'
                    LEFT JOIN member m
                      ON m.org_id = o.id AND m.account_id = :account_id
                     AND m.status = 'active' AND m.role = 'org_admin'
                    WHERE o.status = 'active'
                      AND (:system_admin OR m.id IS NOT NULL)
                    GROUP BY o.id, o.name, o.created_at
                    ORDER BY o.created_at DESC, o.id DESC
                    """
                ),
                {
                    "account_id": identity.account_id,
                    "system_admin": identity.is_system_admin,
                },
            ).mappings().all()
        return [dict(row) for row in rows]

    def authorize_org(self, identity: Identity, org_id: int) -> None:
        if identity.is_system_admin:
            return
        with self.engine.connect() as connection:
            allowed = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM member m JOIN org o ON o.id = m.org_id
                        WHERE m.org_id = :org_id AND m.account_id = :account_id
                          AND m.role = 'org_admin' AND m.status = 'active'
                          AND o.status = 'active'
                    )
                    """
                ),
                {"org_id": org_id, "account_id": identity.account_id},
            ).scalar_one()
        if not allowed:
            raise PermissionError("you do not have access to this organization")

    def organization_settings(
        self,
        identity: Identity,
        org_id: int,
    ) -> dict[str, Any]:
        self.authorize_org(identity, org_id)
        with self.engine.connect() as connection:
            organization = dict(
                connection.execute(
                    text("SELECT id, name, slug, status FROM org WHERE id = :id"),
                    {"id": org_id},
                ).mappings().one()
            )
            sites = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id, name, code, timezone, currency, status
                        FROM site WHERE org_id = :org_id
                        ORDER BY status, id
                        """
                    ),
                    {"org_id": org_id},
                ).mappings()
            ]
            members = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT m.id AS member_id, a.id AS account_id, a.name, a.email,
                            m.role, m.status, m.created_at
                        FROM member m JOIN account a ON a.id = m.account_id
                        WHERE m.org_id = :org_id
                        ORDER BY (m.status = 'active') DESC, lower(a.email)
                        """
                    ),
                    {"org_id": org_id},
                ).mappings()
            ]
            invitations = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id, email, role, status, created_at, expires_at
                        FROM org_invitation
                        WHERE org_id = :org_id AND status = 'pending'
                          AND (expires_at IS NULL OR expires_at > now())
                        ORDER BY created_at DESC
                        """
                    ),
                    {"org_id": org_id},
                ).mappings()
            ]
            policies = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT cp.site_id, c.name, cp.enabled
                        FROM channel_policy cp
                        JOIN channel c ON c.id = cp.channel_id
                        WHERE c.org_id = :org_id
                        """
                    ),
                    {"org_id": org_id},
                ).mappings()
            ]
        enabled_by_site = {
            int(site["id"]): {
                str(policy["name"])
                for policy in policies
                if int(policy["site_id"]) == int(site["id"]) and policy["enabled"]
            }
            for site in sites
        }
        return {
            "organization": organization,
            "sites": sites,
            "members": members,
            "invitations": invitations,
            "enabled_channels": enabled_by_site,
        }

    def rename_organization(
        self,
        identity: Identity,
        org_id: int,
        name: str,
    ) -> None:
        self.authorize_org(identity, org_id)
        name = name.strip()
        if not name:
            raise ValueError("organization name is required")
        with self.engine.begin() as connection:
            old = connection.execute(
                text("SELECT name FROM org WHERE id = :id FOR UPDATE"),
                {"id": org_id},
            ).scalar_one()
            connection.execute(
                text("UPDATE org SET name = :name WHERE id = :id"),
                {"id": org_id, "name": name},
            )
            self._audit(
                connection,
                identity=identity,
                org_id=org_id,
                action="organization.rename",
                object_type="org",
                object_id=org_id,
                change_set={"name": {"from": old, "to": name}},
            )

    def rename_site(
        self,
        identity: Identity,
        org_id: int,
        site_id: int,
        name: str,
    ) -> None:
        self.authorize_org(identity, org_id)
        name = name.strip()
        if not name:
            raise ValueError("site or unit name is required")
        with self.engine.begin() as connection:
            old = connection.execute(
                text(
                    "SELECT name FROM site WHERE id = :site_id AND org_id = :org_id FOR UPDATE"
                ),
                {"site_id": site_id, "org_id": org_id},
            ).scalar_one()
            duplicate = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM site
                        WHERE org_id = :org_id AND id <> :site_id
                          AND lower(name) = lower(:name)
                    )
                    """
                ),
                {"org_id": org_id, "site_id": site_id, "name": name},
            ).scalar_one()
            if duplicate:
                raise ValueError("a site or unit with that name already exists")
            connection.execute(
                text("UPDATE site SET name = :name WHERE id = :site_id"),
                {"site_id": site_id, "name": name},
            )
            self._audit(
                connection,
                identity=identity,
                org_id=org_id,
                site_id=site_id,
                action="site.rename",
                object_type="site",
                object_id=site_id,
                change_set={"name": {"from": old, "to": name}},
            )

    def invite_member(
        self,
        identity: Identity,
        org_id: int,
        email: str,
    ) -> str:
        self.authorize_org(identity, org_id)
        email = normalized_email(email)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE org_invitation SET status = 'expired'
                    WHERE org_id = :org_id AND status = 'pending'
                      AND expires_at IS NOT NULL AND expires_at <= now()
                    """
                ),
                {"org_id": org_id},
            )
            account = connection.execute(
                text(
                    """
                    SELECT id FROM account
                    WHERE lower(email) = :email AND status = 'active'
                      AND email_verified_at IS NOT NULL
                    """
                ),
                {"email": email},
            ).scalar_one_or_none()
            if account is not None:
                connection.execute(
                    text(
                        """
                        INSERT INTO member (org_id, account_id, role, status)
                        VALUES (:org_id, :account_id, 'org_admin', 'active')
                        ON CONFLICT (org_id, account_id) DO UPDATE
                        SET role = 'org_admin', status = 'active'
                        """
                    ),
                    {"org_id": org_id, "account_id": account},
                )
                result = "added"
            else:
                pending = connection.execute(
                    text(
                        """
                        SELECT id FROM org_invitation
                        WHERE org_id = :org_id AND lower(email) = :email
                          AND status = 'pending' FOR UPDATE
                        """
                    ),
                    {"org_id": org_id, "email": email},
                ).scalar_one_or_none()
                if pending is None:
                    connection.execute(
                        text(
                            """
                            INSERT INTO org_invitation (
                                org_id, email, role, status, invited_by, expires_at
                            ) VALUES (
                                :org_id, :email, 'org_admin', 'pending',
                                :invited_by, now() + interval '14 days'
                            )
                            """
                        ),
                        {
                            "org_id": org_id,
                            "email": email,
                            "invited_by": identity.account_id,
                        },
                    )
                result = "invited"
            self._audit(
                connection,
                identity=identity,
                org_id=org_id,
                action=f"member.{result}",
                object_type="member_email",
                object_id=email,
                change_set={"email": email, "role": "org_admin"},
            )
        return result

    def set_member_status(
        self,
        identity: Identity,
        org_id: int,
        member_id: int,
        status: str,
    ) -> None:
        self.authorize_org(identity, org_id)
        if status not in {"active", "revoked"}:
            raise ValueError("unsupported member status")
        with self.engine.begin() as connection:
            member = connection.execute(
                text(
                    """
                    SELECT m.*, a.email FROM member m JOIN account a ON a.id = m.account_id
                    WHERE m.id = :member_id AND m.org_id = :org_id FOR UPDATE
                    """
                ),
                {"member_id": member_id, "org_id": org_id},
            ).mappings().one()
            if status == "revoked" and member["status"] == "active":
                active_count = connection.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM member
                        WHERE org_id = :org_id AND role = 'org_admin' AND status = 'active'
                        """
                    ),
                    {"org_id": org_id},
                ).scalar_one()
                if int(active_count) <= 1:
                    raise ValueError("an organization must keep at least one active administrator")
            connection.execute(
                text("UPDATE member SET status = :status WHERE id = :member_id"),
                {"member_id": member_id, "status": status},
            )
            self._audit(
                connection,
                identity=identity,
                org_id=org_id,
                action="member.disable" if status == "revoked" else "member.enable",
                object_type="member",
                object_id=member_id,
                change_set={"status": {"from": member["status"], "to": status}},
                reason=f"Membership for {member['email']}",
            )

    def revoke_invitation(
        self,
        identity: Identity,
        org_id: int,
        invitation_id: int,
    ) -> None:
        self.authorize_org(identity, org_id)
        with self.engine.begin() as connection:
            invitation = connection.execute(
                text(
                    """
                    UPDATE org_invitation
                    SET status = 'revoked', revoked_at = now()
                    WHERE id = :id AND org_id = :org_id AND status = 'pending'
                    RETURNING email
                    """
                ),
                {"id": invitation_id, "org_id": org_id},
            ).mappings().one_or_none()
            if invitation is None:
                raise ValueError("that invitation is no longer pending")
            self._audit(
                connection,
                identity=identity,
                org_id=org_id,
                action="member.invitation_revoke",
                object_type="org_invitation",
                object_id=invitation_id,
                change_set={"email": invitation["email"]},
            )

    def set_site_channels(
        self,
        identity: Identity,
        org_id: int,
        site_id: int,
        enabled: set[str],
    ) -> None:
        self.authorize_org(identity, org_id)
        unknown = enabled.difference(MVP_CHANNELS)
        if unknown:
            raise ValueError(f"unsupported advertising channels: {', '.join(sorted(unknown))}")
        with self.engine.begin() as connection:
            owns_site = connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM site WHERE id = :site_id AND org_id = :org_id)"),
                {"site_id": site_id, "org_id": org_id},
            ).scalar_one()
            if not owns_site:
                raise PermissionError("that site does not belong to this organization")
            for channel_name in MVP_CHANNELS:
                channel_id = connection.execute(
                    text(
                        """
                        INSERT INTO channel (org_id, name, kind, status)
                        VALUES (:org_id, :name, 'advertising', 'active')
                        ON CONFLICT (org_id, name) DO UPDATE SET status = 'active'
                        RETURNING id
                        """
                    ),
                    {"org_id": org_id, "name": channel_name},
                ).scalar_one()
                connection.execute(
                    text(
                        """
                        INSERT INTO channel_policy (site_id, channel_id, enabled)
                        VALUES (:site_id, :channel_id, :enabled)
                        ON CONFLICT (site_id, channel_id) DO UPDATE
                        SET enabled = EXCLUDED.enabled
                        """
                    ),
                    {
                        "site_id": site_id,
                        "channel_id": channel_id,
                        "enabled": channel_name in enabled,
                    },
                )
            self._audit(
                connection,
                identity=identity,
                org_id=org_id,
                site_id=site_id,
                action="site.channels_update",
                object_type="channel_policy",
                object_id=site_id,
                change_set={"enabled_channels": sorted(enabled)},
            )


__all__ = [
    "Identity",
    "IdentityRepository",
    "MVP_CHANNELS",
    "normalized_email",
    "oidc_identity",
]
