"""A finished reauthentication reloads the entry.

The update listener only reloads on an options change since 2.0.24, so a new
refresh token (and an Xbox Family Settings token handed over by the sign-in)
would otherwise only be read at the next restart.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
import pytest

from custom_components.microsoft_family_safety.config_flow import FamilySafetyConfigFlow


@pytest.mark.parametrize("source", [SOURCE_REAUTH, SOURCE_RECONFIGURE])
async def test_finished_sign_in_reloads_the_entry(source: str) -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": source}
    flow._pending_data = {"refresh_token": "new", "xbox_refresh_token": "xbox"}
    entry = MagicMock(
        entry_id="entry-1",
        options={},
        data={"refresh_token": "old", "auth_user_id": "parent", "web_cookies": [{"name": "x"}]},
    )

    with (
        patch.object(flow, "_get_existing_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", return_value={"type": "abort"}) as update,
    ):
        result = flow._async_finish_reauth()

    assert result == {"type": "abort"}
    # The new tokens replace the old one, and an earlier web session goes.
    assert update.call_args.kwargs["data"] == {
        "refresh_token": "new", "auth_user_id": "parent", "xbox_refresh_token": "xbox"
    }
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


INFO = {"refresh_token": "M.R3_token", "user_id": "1111", "title": "Parent"}


async def test_manual_step_keys_a_new_entry_on_the_user_id() -> None:
    """Audit 2026-11-13: the same account added twice must be refused, its id stored."""
    from homeassistant.config_entries import SOURCE_USER

    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": SOURCE_USER}
    flow.hass.config_entries.async_entry_for_domain_unique_id.return_value = None
    with (
        patch.object(flow, "async_set_unique_id") as set_id,
        patch.object(flow, "async_create_entry", return_value={"type": "create"}) as create,
    ):
        await flow._async_finish_manual_auth(dict(INFO))
    set_id.assert_awaited_once_with("1111", raise_on_progress=False)
    assert create.call_args.kwargs["data"]["auth_user_id"] == "1111"


async def test_manual_step_refuses_another_account_on_reauth() -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": SOURCE_REAUTH}
    entry = MagicMock(data={"auth_user_id": "2222"}, options={})
    with patch.object(flow, "_get_existing_entry", return_value=entry):
        result = await flow._async_finish_manual_auth(dict(INFO))
    assert result["reason"] == "wrong_account"


async def test_manual_step_updates_the_entry_on_reauth() -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": SOURCE_REAUTH}
    entry = MagicMock(entry_id="entry-1", data={"auth_user_id": "1111"}, options={})
    with (
        patch.object(flow, "_get_existing_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", return_value={"type": "abort"}) as update,
    ):
        await flow._async_finish_manual_auth(dict(INFO))
    data = update.call_args.kwargs["data"]
    assert data["refresh_token"] == "M.R3_token" and "web_cookies" not in data
