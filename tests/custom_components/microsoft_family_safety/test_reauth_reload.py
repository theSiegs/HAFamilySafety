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
