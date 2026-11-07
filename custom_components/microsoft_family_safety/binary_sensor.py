"""Binary sensor platform for Microsoft Family Safety: may the child play now?"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_PLATFORM, ATTR_USER_ID, DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator
from .entity_base import ChildEntityMixin, MinuteUpdateMixin
from .screen_time import downtime_window

#: Platform -> the account data key holding its schedule.
SCHEDULE_KEYS = {"Windows": "screentime_policy", "Xbox": "xbox_screentime_policy"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the "allowed now" binary sensors."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[tuple[str, str]] = set()

    def _add_new_entities() -> None:
        entities: list[BinarySensorEntity] = []
        for account_id, account_data in (coordinator.data or {}).get("accounts", {}).items():
            for platform, key in SCHEDULE_KEYS.items():
                if (account_id, platform) in known or not isinstance(
                    (account_data or {}).get(key), dict
                ):
                    continue
                known.add((account_id, platform))
                entities.append(
                    FamilySafetyAllowedNowSensor(coordinator, entry, account_id, platform)
                )
                entities.append(
                    FamilySafetyDowntimeSensor(coordinator, entry, account_id, platform)
                )
        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class FamilySafetyAllowedNowSensor(
    ChildEntityMixin, MinuteUpdateMixin, CoordinatorEntity, BinarySensorEntity
):
    """On while the child may use the platform right now.

    The minutes left and used move every minute: they are not recorded.

    Off when a lock is in force, outside today's allowed hours, or once today's
    allowance is used up; on with limits turned off or extra time granted.
    Unknown while the schedule or today's usage is unknown.
    """

    _unrecorded_attributes = frozenset({"remaining_minutes", "used_minutes"})

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._platform = platform
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_{platform.lower()}_allowed_now"
        self._attr_name = f"{self._first_name()} {platform} Allowed Now"
        self._attr_icon = "mdi:microsoft-xbox" if platform == "Xbox" else "mdi:microsoft-windows"



    @property
    def is_on(self) -> bool | None:
        """True while the platform may be used now."""
        return self.coordinator.platform_status(self._account_id, self._platform)["allowed"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Why, and the minutes left."""
        status = self.coordinator.platform_status(self._account_id, self._platform)
        return {
            ATTR_USER_ID: self._account_id,
            ATTR_PLATFORM: self._platform,
            "reason": status["reason"],
            "remaining_minutes": status["remaining_minutes"],
            "allowance_minutes": status["allowance_minutes"],
            "used_minutes": status["used_minutes"],
            "in_allowed_hours": status["in_allowed_hours"],
            # Who decides the lock now (ours / parent / none) and what is in
            # force: override, override_until and allow_until (local ISO).
            **self.coordinator.platform_hold_state(self._account_id, self._platform),
        }


class FamilySafetyDowntimeSensor(
    ChildEntityMixin, MinuteUpdateMixin, CoordinatorEntity, BinarySensorEntity
):
    """On while the current local time is outside today's allowed hours.

    ``downtime_start`` / ``downtime_end`` give the current downtime, or the
    next one, as local ISO times. The same values are also given as
    ``bedtime_start`` / ``bedtime_end``, the names of Family Link's Bedtime
    Active sensor, which Hearth reads to know when a lock starts and ends.
    Off with limits turned off; unknown while the schedule is unknown.
    """

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._platform = platform
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_{platform.lower()}_downtime"
        self._attr_name = f"{self._first_name()} {platform} Downtime Active"
        self._attr_icon = "mdi:sleep"


    def _window(self) -> dict[str, Any]:
        policy = self._account_data().get(SCHEDULE_KEYS[self._platform])
        return downtime_window(policy if isinstance(policy, dict) else None, dt_util.now())


    @property
    def is_on(self) -> bool | None:
        """True during downtime."""
        return self._window()["active"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The current or next downtime."""
        window = self._window()
        start = window["start"].isoformat() if window["start"] else None
        end = window["end"].isoformat() if window["end"] else None
        return {
            ATTR_USER_ID: self._account_id,
            ATTR_PLATFORM: self._platform,
            "downtime_start": start,
            "downtime_end": end,
            "bedtime_start": start,
            "bedtime_end": end,
        }
