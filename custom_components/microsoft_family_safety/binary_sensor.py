"""Binary sensor platform for Microsoft Family Safety: may the child play now?"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_FIRST_NAME, ATTR_PLATFORM, ATTR_SURNAME, ATTR_USER_ID, DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator

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
        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class MinuteUpdateMixin:
    """Re-evaluate every minute: allowed hours and time left move with the clock."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(self.hass, self._async_minute_tick, second=0)
        )

    _last_snapshot: Any = None

    def _snapshot(self) -> Any:
        """What the entity shows; the tick writes only when it changed."""
        value = self.is_on if isinstance(self, BinarySensorEntity) else self.native_value
        return value, tuple(sorted((self.extra_state_attributes or {}).items(), key=str))

    @callback
    def _async_minute_tick(self, _now: datetime) -> None:
        snapshot = self._snapshot()
        if snapshot != self._last_snapshot:
            self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        self._last_snapshot = self._snapshot()
        super().async_write_ha_state()


class FamilySafetyAllowedNowSensor(MinuteUpdateMixin, CoordinatorEntity, BinarySensorEntity):
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
        self._attr_name = f"{self._account_data().get(ATTR_FIRST_NAME, 'Unknown')} {platform} Allowed Now"
        self._attr_icon = "mdi:microsoft-xbox" if platform == "Xbox" else "mdi:microsoft-windows"

    def _account_data(self) -> dict[str, Any]:
        return ((self.coordinator.data or {}).get("accounts") or {}).get(self._account_id) or {}

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the child's device."""
        account = self._account_data()
        full_name = f"{account.get(ATTR_FIRST_NAME, 'Unknown')} {account.get(ATTR_SURNAME, '')}".strip()
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{full_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )

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
            "override": (status.get("override") or {}).get("type"),
            "override_until": (status.get("override") or {}).get("valid_until"),
        }
