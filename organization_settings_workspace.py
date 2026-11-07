"""Organization-admin settings for names, members, sites, and channels."""

from __future__ import annotations

from typing import Any

import streamlit as st

from database_identity import Identity, IdentityRepository, MVP_CHANNELS
from gateway_client import GatewayIdentityService


def _repository(identity: Identity):
    return GatewayIdentityService(identity) if identity.gateway_token else IdentityRepository()


def _save_org_name(identity: Identity, org_id: int, name: str) -> None:
    repository = _repository(identity)
    try:
        repository.rename_organization(identity, org_id, name)
    finally:
        repository.close()


def _save_site_name(identity: Identity, org_id: int, site_id: int, name: str) -> None:
    repository = _repository(identity)
    try:
        repository.rename_site(identity, org_id, site_id, name)
    finally:
        repository.close()


def _render_general(
    identity: Identity,
    org_id: int,
    workspace: dict[str, Any],
) -> None:
    organization = workspace["organization"]
    with st.form(f"organization_name_{org_id}"):
        name = st.text_input("Organization name", value=str(organization["name"]))
        if st.form_submit_button("Save organization name", type="primary"):
            try:
                _save_org_name(identity, org_id, name)
            except (PermissionError, ValueError) as error:
                st.error(str(error))
            else:
                st.success("Organization name updated.")
                st.session_state.pop("client_org_picker", None)
                st.rerun()

    st.markdown("### Sites / Units")
    for site in workspace["sites"]:
        site_id = int(site["id"])
        with st.form(f"site_name_{site_id}"):
            columns = st.columns([3, 1])
            name = columns[0].text_input(
                "Site / Unit name",
                value=str(site["name"]),
                key=f"site_name_value_{site_id}",
            )
            columns[1].caption(f"{site['timezone']} · {site['currency']}")
            if st.form_submit_button("Save name"):
                try:
                    _save_site_name(identity, org_id, site_id, name)
                except (PermissionError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.success("Site / Unit name updated.")
                    st.session_state.pop("client_site_picker", None)
                    st.rerun()


def _render_members(
    identity: Identity,
    org_id: int,
    workspace: dict[str, Any],
) -> None:
    st.caption(
        "Every active customer user is an organization administrator in the MVP. "
        "Disabled users keep their audit history but lose access immediately."
    )
    with st.form(f"invite_member_{org_id}", clear_on_submit=True):
        email = st.text_input("Administrator email", placeholder="name@example.com")
        if st.form_submit_button("Add administrator", type="primary"):
            repository = _repository(identity)
            try:
                result = repository.invite_member(identity, org_id, email)
            except (PermissionError, ValueError) as error:
                st.error(str(error))
            else:
                if result == "added":
                    st.success("The verified user now has access.")
                else:
                    st.success(
                        "Invitation recorded. Access activates when that verified email signs in."
                    )
                st.rerun()
            finally:
                repository.close()

    for member in workspace["members"]:
        active = member["status"] == "active"
        with st.container(border=True):
            left, right = st.columns([4, 1])
            left.markdown(f"**{member['name']}**")
            left.caption(f"{member['email']} · {'Active' if active else 'Disabled'}")
            label = "Disable" if active else "Restore"
            if right.button(
                label,
                key=f"member_status_{member['member_id']}_{member['status']}",
                use_container_width=True,
            ):
                repository = _repository(identity)
                try:
                    repository.set_member_status(
                        identity,
                        org_id,
                        int(member["member_id"]),
                        "revoked" if active else "active",
                    )
                except (PermissionError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.rerun()
                finally:
                    repository.close()

    if workspace["invitations"]:
        st.markdown("### Pending invitations")
    for invitation in workspace["invitations"]:
        with st.container(border=True):
            left, right = st.columns([4, 1])
            left.markdown(f"**{invitation['email']}**")
            left.caption("Waiting for a verified sign-in · expires after 14 days")
            if right.button(
                "Revoke",
                key=f"revoke_invitation_{invitation['id']}",
                use_container_width=True,
            ):
                repository = _repository(identity)
                try:
                    repository.revoke_invitation(
                        identity,
                        org_id,
                        int(invitation["id"]),
                    )
                except (PermissionError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.rerun()
                finally:
                    repository.close()


def _render_channels(
    identity: Identity,
    org_id: int,
    workspace: dict[str, Any],
) -> None:
    st.caption(
        "Choose which advertising channels can appear in spend plans for each Site / Unit."
    )
    for site in workspace["sites"]:
        if site["status"] != "active":
            continue
        site_id = int(site["id"])
        with st.form(f"site_channels_{site_id}"):
            selected = st.multiselect(
                f"{site['name']} channels",
                options=list(MVP_CHANNELS),
                default=sorted(workspace["enabled_channels"].get(site_id, set())),
            )
            if st.form_submit_button("Save permitted channels"):
                repository = _repository(identity)
                try:
                    repository.set_site_channels(
                        identity,
                        org_id,
                        site_id,
                        set(selected),
                    )
                except (PermissionError, ValueError) as error:
                    st.error(str(error))
                else:
                    st.success(f"Permitted channels updated for {site['name']}.")
                    st.rerun()
                finally:
                    repository.close()


def render_organization_settings(identity: Identity, org_id: int) -> None:
    repository = _repository(identity)
    try:
        workspace = repository.organization_settings(identity, org_id)
    except (PermissionError, ValueError) as error:
        st.error(str(error))
        return
    finally:
        repository.close()

    st.markdown('<div class="dashboard-kicker">Administration</div>', unsafe_allow_html=True)
    st.markdown('<h1 class="dashboard-title">Organization settings</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dashboard-subtitle">Manage customer access, names, and permitted advertising channels.</div>',
        unsafe_allow_html=True,
    )
    general, members, channels = st.tabs(["Organization & sites", "Administrators", "Ad channels"])
    with general:
        _render_general(identity, org_id, workspace)
    with members:
        _render_members(identity, org_id, workspace)
    with channels:
        _render_channels(identity, org_id, workspace)


__all__ = ["render_organization_settings"]
