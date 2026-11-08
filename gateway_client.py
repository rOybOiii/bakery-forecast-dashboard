"""Authenticated client for the hosted Credent gateway.

The Streamlit process uses this module instead of database credentials for
identity, tenant navigation, and organization administration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import date
import os
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pandas as pd


LOCAL_ISSUER = "https://local.credent.invalid/"
LOCAL_AUDIENCE = "credent-gateway"
LOCAL_DEV_SECRET = "credent-local-development-only-change-before-hosting"


class GatewayError(RuntimeError):
    """A safe, user-facing error returned by the gateway boundary."""


def configured_gateway_url(*, production: bool = False) -> str | None:
    value = os.environ.get("CREDENT_API_URL", "").strip().rstrip("/")
    if value:
        if production and not value.startswith("https://"):
            return None
        return value
    return None if production else "http://127.0.0.1:63100"


def local_development_token(
    *,
    subject: str = "local-streamlit-manager",
    email: str = "local-streamlit-manager@credent.local",
    name: str = "Local Streamlit manager",
) -> str:
    """Mint the same short-lived development identity trusted by the local API."""

    secret = os.environ.get("CREDENT_GATEWAY_DEV_SECRET") or LOCAL_DEV_SECRET
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": LOCAL_ISSUER,
            "aud": LOCAL_AUDIENCE,
            "sub": subject,
            "email": email.casefold(),
            "email_verified": True,
            "name": name,
            "credent_kind": "user",
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "jti": str(uuid4()),
        },
        secret,
        algorithm="HS256",
    )


class GatewayClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Request-ID": str(uuid4()),
                },
                json=json,
                timeout=self.timeout,
            )
        except httpx.HTTPError as error:
            raise GatewayError(
                "Credent's secure service is temporarily unavailable. Please try again."
            ) from error
        if response.is_error:
            message = "The secure service could not complete that request."
            try:
                detail = response.json().get("detail")
                if isinstance(detail, str) and detail:
                    message = detail
            except (ValueError, AttributeError):
                pass
            raise GatewayError(message)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def me(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/me"))

    def organizations(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/v1/organizations")["items"])

    def create_organization(
        self,
        *,
        name: str,
        site_names: list[str],
        timezone: str,
        currency: str = "USD",
    ) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                "/v1/organizations",
                json={
                    "name": name,
                    "site_names": site_names,
                    "timezone": timezone,
                    "currency": currency,
                },
            )
        )

    def sites(self, org_id: int) -> list[dict[str, Any]]:
        return list(
            self._request("GET", f"/v1/organizations/{org_id}/sites")["items"]
        )

    def add_sites(
        self,
        org_id: int,
        *,
        site_names: list[str],
        timezone: str,
        currency: str,
    ) -> list[int]:
        result = self._request(
            "POST",
            f"/v1/organizations/{org_id}/sites",
            json={
                "site_names": site_names,
                "timezone": timezone,
                "currency": currency,
            },
        )
        return [int(value) for value in result["site_ids"]]

    def organization_settings(self, org_id: int) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/organizations/{org_id}/settings"))

    def rename_organization(self, org_id: int, name: str) -> None:
        self._request("PATCH", f"/v1/organizations/{org_id}", json={"name": name})

    def rename_site(self, org_id: int, site_id: int, name: str) -> None:
        self._request(
            "PATCH",
            f"/v1/organizations/{org_id}/sites/{site_id}",
            json={"name": name},
        )

    def set_site_onboarding_skipped(
        self,
        org_id: int,
        site_id: int,
        *,
        skipped: bool,
    ) -> None:
        self._request(
            "PUT",
            f"/v1/organizations/{org_id}/sites/{site_id}/onboarding-skip",
            json={"skipped": bool(skipped)},
        )

    def invite_member(self, org_id: int, email: str) -> str:
        result = self._request(
            "POST",
            f"/v1/organizations/{org_id}/invitations",
            json={"email": email},
        )
        return str(result["status"])

    def revoke_invitation(self, org_id: int, invitation_id: int) -> None:
        self._request(
            "DELETE",
            f"/v1/organizations/{org_id}/invitations/{invitation_id}",
        )

    def set_member_status(self, org_id: int, member_id: int, status: str) -> None:
        self._request(
            "PATCH",
            f"/v1/organizations/{org_id}/members/{member_id}",
            json={"status": status},
        )

    def set_site_channels(
        self,
        org_id: int,
        site_id: int,
        enabled: set[str],
    ) -> None:
        self._request(
            "PUT",
            f"/v1/organizations/{org_id}/sites/{site_id}/channels",
            json={"enabled": sorted(enabled)},
        )

    def sales_workspace(
        self, org_id: int, site_id: int, metric_id: int
    ) -> dict[str, Any]:
        return dict(
            self._request(
                "GET",
                f"/v1/organizations/{org_id}/sites/{site_id}/metrics/{metric_id}/sales",
            )
        )

    def job_monitoring(self, org_id: int) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/organizations/{org_id}/jobs"))

    def cancel_job(self, org_id: int, run_id: int) -> str:
        result = self._request(
            "POST", f"/v1/organizations/{org_id}/jobs/{run_id}/cancel"
        )
        return str(result["status"])

    def rerun_first_forecast(
        self,
        org_id: int,
        site_id: int,
        metric_id: int,
    ) -> tuple[int, bool]:
        result = self._request(
            "POST",
            f"/v1/organizations/{org_id}/sites/{site_id}/metrics/{metric_id}/"
            "forecasts/rerun",
        )
        return int(result["run_id"]), bool(result["created"])

    def preview_sales(
        self,
        org_id: int,
        site_id: int,
        metric_id: int,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                f"/v1/organizations/{org_id}/sites/{site_id}/metrics/{metric_id}/sales/preview",
                json={"rows": rows},
            )
        )

    def apply_sales(
        self,
        org_id: int,
        site_id: int,
        metric_id: int,
        *,
        rows: list[dict[str, Any]],
        filename: str,
        payload: bytes,
        reason: str,
    ) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                f"/v1/organizations/{org_id}/sites/{site_id}/metrics/{metric_id}/sales/apply",
                json={
                    "rows": rows,
                    "filename": filename,
                    "payload_base64": base64.b64encode(payload).decode("ascii"),
                    "reason": reason,
                },
            )
        )

    def application_operation(
        self,
        org_id: int,
        site_id: int,
        metric_id: int,
        domain: str,
        operation: str,
        *,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        payload = self._request(
            "POST",
            f"/v1/organizations/{org_id}/sites/{site_id}/metrics/{metric_id}/application/"
            f"{domain}/{operation}",
            json={
                "args": _transport_encode(list(args)),
                "kwargs": _transport_encode(kwargs or {}),
            },
        )
        return _transport_decode(payload.get("result"))


def _transport_encode(value: Any) -> Any:
    """Encode application values without granting the client arbitrary RPC types."""

    if isinstance(value, pd.DataFrame):
        safe = value.copy()
        return {
            "__credent_type__": "dataframe",
            "records": _transport_encode(safe.to_dict(orient="records")),
            "columns": [str(column) for column in safe.columns],
        }
    if not isinstance(value, (str, bytes, dict, list, tuple, set)):
        try:
            if bool(pd.isna(value)):
                return None
        except (TypeError, ValueError):
            pass
    if isinstance(value, pd.Timestamp):
        return {"__credent_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, datetime):
        return {"__credent_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__credent_type__": "date", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {
            "__credent_type__": "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    if is_dataclass(value):
        return _transport_encode(asdict(value))
    if isinstance(value, dict):
        return {str(key): _transport_encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_transport_encode(item) for item in value]
    return value


def _transport_decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_transport_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    marker = value.get("__credent_type__")
    if marker == "dataframe":
        return pd.DataFrame(
            _transport_decode(value.get("records", [])),
            columns=value.get("columns"),
        )
    if marker == "date":
        return date.fromisoformat(str(value["value"]))
    if marker == "datetime":
        return datetime.fromisoformat(str(value["value"]))
    if marker == "bytes":
        return base64.b64decode(str(value["value"]))
    return {key: _transport_decode(item) for key, item in value.items()}


_ACTIVE_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "credent_gateway_scope", default=None
)
_SESSION_SCOPE_KEY = "_credent_gateway_scope"


def _store_session_scope(scope: dict[str, Any] | None) -> None:
    """Persist only primitive gateway context for Streamlit fragment reruns."""

    try:
        import streamlit as st
    except ImportError:
        return
    if scope is None:
        st.session_state.pop(_SESSION_SCOPE_KEY, None)
        return
    client = scope["client"]
    st.session_state[_SESSION_SCOPE_KEY] = {
        "base_url": client.base_url,
        "token": client.token,
        "org_id": int(scope["org_id"]),
        "site_id": int(scope["site_id"]),
        "metric_id": int(scope["metric_id"]),
    }


def bind_gateway_scope(
    identity: Any,
    *,
    org_id: int,
    site_id: int,
    metric_id: int,
) -> None:
    """Bind the authenticated tenant scope for this Streamlit rerun thread."""

    if identity.gateway_url and identity.gateway_token:
        scope = {
            "client": GatewayClient(identity.gateway_url, identity.gateway_token),
            "org_id": int(org_id),
            "site_id": int(site_id),
            "metric_id": int(metric_id),
        }
        _ACTIVE_SCOPE.set(scope)
        _store_session_scope(scope)
    else:
        _ACTIVE_SCOPE.set(None)
        _store_session_scope(None)


def active_gateway_scope() -> dict[str, Any] | None:
    scope = _ACTIVE_SCOPE.get()
    if scope is not None:
        return scope
    try:
        import streamlit as st
    except ImportError:
        return None
    stored = st.session_state.get(_SESSION_SCOPE_KEY)
    if not isinstance(stored, dict):
        return None
    required = {"base_url", "token", "org_id", "site_id", "metric_id"}
    if not required.issubset(stored):
        return None
    restored = {
        "client": GatewayClient(str(stored["base_url"]), str(stored["token"])),
        "org_id": int(stored["org_id"]),
        "site_id": int(stored["site_id"]),
        "metric_id": int(stored["metric_id"]),
    }
    _ACTIVE_SCOPE.set(restored)
    return restored


class _GatewayApplicationFacade:
    def __init__(self, domain: str) -> None:
        scope = active_gateway_scope()
        if scope is None:
            raise GatewayError("The secure Credent service is not bound to this page.")
        self.domain = domain
        self.scope = scope

    def close(self) -> None:
        return None

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        return self.scope["client"].application_operation(
            self.scope["org_id"],
            self.scope["site_id"],
            self.scope["metric_id"],
            self.domain,
            operation,
            args=args,
            kwargs=kwargs,
        )


class GatewayOnboardingRepository(_GatewayApplicationFacade):
    def __init__(self) -> None:
        super().__init__("onboarding")

    def __getattr__(self, operation: str):
        return lambda *args, **kwargs: self._call(operation, *args, **kwargs)

    def store_original_upload(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        result = self._call("store_original_upload", *args, **kwargs)
        return SimpleNamespace(**result)


class GatewayPipelineRepository(_GatewayApplicationFacade):
    def __init__(self) -> None:
        super().__init__("pipeline")

    def __getattr__(self, operation: str):
        return lambda *args, **kwargs: self._call(operation, *args, **kwargs)


class GatewayWorkflowController(_GatewayApplicationFacade):
    def __init__(self) -> None:
        super().__init__("controller")

    def __getattr__(self, operation: str):
        return lambda *args, **kwargs: self._call(operation, *args, **kwargs)


def application_call(domain: str, operation: str, *args: Any, **kwargs: Any) -> Any:
    return _GatewayApplicationFacade(domain)._call(operation, *args, **kwargs)


class GatewayIdentityService:
    """Compatibility facade while Streamlit pages migrate off repositories."""

    def __init__(self, identity: Any) -> None:
        if not identity.gateway_token:
            raise GatewayError("The signed-in session does not have an API credential.")
        url = identity.gateway_url or configured_gateway_url(
            production=os.environ.get("CREDENT_ENV", "development").casefold()
            in {"production", "prod"}
        )
        if not url:
            raise GatewayError("The secure Credent service is not configured.")
        self.client = GatewayClient(url, identity.gateway_token)

    def close(self) -> None:
        return None

    def organizations_for(self, _identity: Any) -> list[dict[str, Any]]:
        return self.client.organizations()

    def authorize_org(self, _identity: Any, org_id: int) -> None:
        # The gateway performs the authoritative check; this request is only a
        # cheap early check for pages that previously called a repository guard.
        self.client.sites(org_id)

    def sites_for_org(self, org_id: int) -> list[dict[str, Any]]:
        return self.client.sites(org_id)

    def create_organization(
        self,
        *,
        business_name: str,
        site_names: list[str],
        timezone: str,
        owner_account_id: int | None = None,
        currency: str = "USD",
    ) -> dict[str, Any]:
        del owner_account_id
        return self.client.create_organization(
            name=business_name,
            site_names=site_names,
            timezone=timezone,
            currency=currency,
        )

    def add_sites(
        self,
        *,
        org_id: int,
        site_names: list[str],
        timezone: str,
        currency: str,
    ) -> list[int]:
        return self.client.add_sites(
            org_id,
            site_names=site_names,
            timezone=timezone,
            currency=currency,
        )

    def set_site_onboarding_skipped(
        self,
        *,
        org_id: int,
        site_id: int,
        skipped: bool,
    ) -> None:
        self.client.set_site_onboarding_skipped(
            org_id,
            site_id,
            skipped=skipped,
        )

    def organization_settings(self, _identity: Any, org_id: int) -> dict[str, Any]:
        result = self.client.organization_settings(org_id)
        result["enabled_channels"] = {
            int(site_id): set(values)
            for site_id, values in result.get("enabled_channels", {}).items()
        }
        return result

    def rename_organization(self, _identity: Any, org_id: int, name: str) -> None:
        self.client.rename_organization(org_id, name)

    def rename_site(
        self, _identity: Any, org_id: int, site_id: int, name: str
    ) -> None:
        self.client.rename_site(org_id, site_id, name)

    def invite_member(self, _identity: Any, org_id: int, email: str) -> str:
        return self.client.invite_member(org_id, email)

    def revoke_invitation(
        self, _identity: Any, org_id: int, invitation_id: int
    ) -> None:
        self.client.revoke_invitation(org_id, invitation_id)

    def set_member_status(
        self,
        _identity: Any,
        org_id: int,
        member_id: int,
        status: str,
    ) -> None:
        self.client.set_member_status(org_id, member_id, status)

    def set_site_channels(
        self,
        _identity: Any,
        org_id: int,
        site_id: int,
        enabled: set[str],
    ) -> None:
        self.client.set_site_channels(org_id, site_id, enabled)


__all__ = [
    "GatewayOnboardingRepository",
    "GatewayPipelineRepository",
    "GatewayWorkflowController",
    "GatewayClient",
    "GatewayError",
    "GatewayIdentityService",
    "active_gateway_scope",
    "application_call",
    "bind_gateway_scope",
    "configured_gateway_url",
    "local_development_token",
]
