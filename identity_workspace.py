"""Streamlit identity boundary backed by OIDC and Credent authorization."""

from __future__ import annotations

import os
from typing import Any

import streamlit as st

from database_identity import Identity
from gateway_client import (
    GatewayClient,
    GatewayError,
    configured_gateway_url,
    local_development_token,
)


def _auth_configured() -> bool:
    try:
        auth = st.secrets.get("auth")
    except Exception:
        return False
    return bool(auth)


def _production_mode() -> bool:
    return os.environ.get("CREDENT_ENV", "development").strip().casefold() in {
        "production",
        "prod",
    }


def _gateway_url() -> str | None:
    try:
        configured = str(st.secrets.get("credent", {}).get("api_url", "")).strip()
    except Exception:
        configured = ""
    if configured:
        if _production_mode() and not configured.startswith("https://"):
            return None
        return configured.rstrip("/")
    return configured_gateway_url(production=_production_mode())


def _gateway_access_token(tokens: Any | None = None) -> str:
    """Return the Auth0 API access token exposed by Streamlit."""

    try:
        token_source = st.user.tokens if tokens is None else tokens
        token = token_source["access"]
    except (AttributeError, KeyError, TypeError) as error:
        raise GatewayError(
            "Sign-in did not return the API access token required by Credibly. "
            "Please sign out and try again."
        ) from error
    if not isinstance(token, str) or not token.strip():
        raise GatewayError(
            "Sign-in returned an invalid API access token. Please sign out and try again."
        )
    return token.strip()


def render_identity_gate() -> Identity | None:
    """Require identity before any organization or customer data is loaded."""

    gateway_url = _gateway_url()
    if gateway_url is None:
        st.error("The secure Credibly service is not configured.")
        return None
    if _auth_configured():
        if not st.user.is_logged_in:
            st.markdown(
                '<div class="dashboard-kicker">Secure access</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<h1 class="dashboard-title">Sign in to Forecasting Credib.ly</h1>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="dashboard-subtitle">Use your verified account to '
                "access your organization.</div>",
                unsafe_allow_html=True,
            )
            st.button(
                "Sign in or create an account",
                type="primary",
                on_click=st.login,
                use_container_width=True,
            )
            return None
        try:
            token = _gateway_access_token()
            account = GatewayClient(gateway_url, token).me()
            return Identity(
                account_id=int(account["account_id"]),
                auth_id=str(account["auth_id"]),
                name=str(account["name"]),
                email=str(account["email"]),
                system_role=str(account["system_role"]),
                gateway_token=token,
                gateway_url=gateway_url,
            )
        except (GatewayError, KeyError, TypeError) as error:
            st.error(str(error))
            st.button("Sign out", on_click=st.logout)
            return None
    if _production_mode():
        st.error(
            "Sign-in is not configured. Add the hosted OIDC settings before "
            "starting Forecasting Credib.ly in production."
        )
        return None
    try:
        token = local_development_token(
            subject=os.environ.get(
                "CREDENT_LOCAL_AUTH_ID", "local-streamlit-manager"
            )
        )
        account = GatewayClient(gateway_url, token).me()
        identity = Identity(
            account_id=int(account["account_id"]),
            auth_id=str(account["auth_id"]),
            name=str(account["name"]),
            email=str(account["email"]),
            system_role=str(account["system_role"]),
            gateway_token=token,
            gateway_url=gateway_url,
        )
    except GatewayError as error:
        st.error(str(error))
        return None
    st.session_state.credent_local_identity = True
    return identity


def render_identity_sidebar(identity: Identity) -> None:
    with st.sidebar:
        st.markdown("### Signed in")
        st.markdown(f"**{identity.name}**")
        st.caption(identity.email)
        if identity.is_system_admin:
            st.caption("System administrator")
        else:
            st.caption("Organization administrator")
        if st.session_state.get("credent_local_identity"):
            st.info("Local development identity")
        elif _auth_configured():
            st.button("Sign out", on_click=st.logout, use_container_width=True)


__all__ = ["render_identity_gate", "render_identity_sidebar"]
