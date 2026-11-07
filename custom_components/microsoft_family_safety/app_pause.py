"""pause_app / resume_app for a child's Windows and Xbox apps.

Mirrors Family Link's app_pause.py: the state an app had before its first
pause is saved once (a repeat pause does not overwrite it, also across a
restart), and resume_app puts it back and forgets it.

Family Link pauses with a 1-minute daily limit, because blocking an Android
app hides it (and lost the texts of a blocked SMS app). Here a pause is a
block (the existing block_app write): Windows and Xbox apps stay installed
when blocked, and the prior app limit cannot be restored faithfully, since
the shape of the limit in Microsoft's app activity report has never been
captured. So the saved state is whether the app was blocked:

* an app already blocked before the pause is skipped and left as it is;
* resume unblocks an app this pause blocked.

App ids are Microsoft's, with the platform prefix (x: Xbox, appx: and win32:
Windows), as the app block switches and the ``apps`` attribute show them.
"""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .targeting import child_schema

STORAGE_VERSION = 1

_APP_LIST = vol.All(cv.ensure_list, [cv.string], vol.Length(min=1))


def _apps_alias(data: dict[str, Any]) -> dict[str, Any]:
    """``packages`` (Family Link's field name) is accepted for ``apps``."""
    if "packages" in data:
        data.setdefault("apps", data.pop("packages"))
    return data


PAUSE_SCHEMA = vol.All(
    _apps_alias,
    child_schema({vol.Required("apps"): _APP_LIST}),
)
RESUME_SCHEMA = vol.All(
    _apps_alias,
    child_schema({vol.Optional("apps"): _APP_LIST}),
)


class AppPauser:
    """The saved states and the two operations, one at a time per entry."""

    def __init__(self, hass, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.app_pause.{entry_id}")
        self._lock = asyncio.Lock()

    async def async_saved(self, account_id: str) -> dict[str, Any]:
        """app id -> saved state, for one child."""
        data = await self._store.async_load() or {}
        return dict(data.get(str(account_id)) or {})

    async def async_pause(self, coordinator, account_id: str, apps: list[str]) -> dict[str, Any]:
        account_id = str(account_id)
        async with self._lock:
            data = await self._store.async_load() or {}
            saved: dict[str, Any] = data.setdefault(account_id, {})
            paused: list[str] = []
            skipped: dict[str, str] = {}
            for app_id in dict.fromkeys(apps):
                if app_id in saved:
                    # Paused already: block again in case it was released
                    # elsewhere, but keep the state saved by the first pause.
                    pass
                else:
                    app = coordinator.get_application(account_id, app_id)
                    if app is None:
                        skipped[app_id] = "unknown app"
                        continue
                    if app.blocked:
                        skipped[app_id] = "already blocked"
                        continue
                    saved[app_id] = {"blocked": False}
                    # Saved before the write: a block that went through but
                    # answered an error must still be resumable.
                    await self._store.async_save(data)
                try:
                    await coordinator.async_block_app(account_id, app_id)
                except Exception as err:  # noqa: BLE001 - reported per app
                    skipped[app_id] = f"block failed: {err}"[:200]
                    continue
                paused.append(app_id)
            if not saved:
                data.pop(account_id, None)
            await self._store.async_save(data)
        return {"account_id": account_id, "paused": paused, "skipped": skipped}

    async def async_resume(
        self, coordinator, account_id: str, apps: list[str] | None
    ) -> dict[str, Any]:
        account_id = str(account_id)
        async with self._lock:
            data = await self._store.async_load() or {}
            saved: dict[str, Any] = data.get(account_id) or {}
            resumed: list[str] = []
            skipped: dict[str, str] = {}
            for app_id in dict.fromkeys(apps if apps else list(saved)):
                state = saved.get(app_id)
                if state is None:
                    skipped[app_id] = "not paused"
                    continue
                if not state.get("blocked"):
                    try:
                        await coordinator.async_unblock_app(account_id, app_id)
                    except Exception as err:  # noqa: BLE001 - kept for a later resume
                        skipped[app_id] = f"unblock failed: {err}"[:200]
                        continue
                saved.pop(app_id, None)
                resumed.append(app_id)
            if saved:
                data[account_id] = saved
            else:
                data.pop(account_id, None)
            await self._store.async_save(data)
        return {"account_id": account_id, "resumed": resumed, "skipped": skipped}
