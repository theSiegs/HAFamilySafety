"""add_time_bonus / cancel_time_bonus on Windows: mobile device-limits overrides only."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"
FAR = "2099-01-01T00:00:00.000+0000"


def _iso(when) -> str:
    return dt_util.as_utc(when).strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _state(*overrides: tuple[str, str], name: str = "Windows") -> dict:
    return {
        "lockablePlatforms": [
            {
                "appliesTo": name,
                "overrides": [{"overrideType": t, "validUntil": u} for t, u in overrides],
                "devices": [],
            },
            {"appliesTo": "Xbox", "overrides": [], "devices": []},
        ]
    }


class FakeOverrides:
    """The mobile overrides endpoint: a GET answers ``reads`` in turn (the last repeats)."""

    def __init__(self, *reads):
        self.reads = list(reads)
        self.posts: list[tuple] = []
        self.post_error: Exception | None = None
        self.cancel_error: Exception | None = None

    async def get(self, child_id):
        assert child_id == CHILD
        answer = self.reads[0] if len(self.reads) == 1 else self.reads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def post(self, child_id, target, override_type, valid_until, culture="en-US"):
        self.posts.append((child_id, target, override_type, valid_until))
        if override_type == "Cancel" and self.cancel_error is not None:
            raise self.cancel_error
        if override_type != "Cancel" and self.post_error is not None:
            raise self.post_error
        return {}

    @property
    def types(self) -> list[str]:
        return [p[2] for p in self.posts]


@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()
    coord._accounts = {int(CHILD): SimpleNamespace(user_id=int(CHILD), first_name="Sam", surname="Doe")}
    coord.xbox_family = MagicMock()
    coord.xbox_family.async_create_override = AsyncMock()
    # Only the mobile overrides; any web call would be a MagicMock, not awaited.
    coord.web_api = MagicMock()
    return coord


def _serve(coord, fake: FakeOverrides) -> FakeOverrides:
    coord.web_api.get_mobile_device_overrides = AsyncMock(side_effect=fake.get)
    coord.web_api.set_mobile_device_override = AsyncMock(side_effect=fake.post)
    return fake


# --- add ----------------------------------------------------------------------------
async def test_windows_extra_time_confirmed_by_the_read_back(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    fake = _serve(coordinator, FakeOverrides(_state(), _state(("AllowUntil", _iso(until)))))

    answer = await coordinator.async_add_time_bonus(CHILD, "Windows", until)

    assert fake.posts == [(CHILD, "Windows", "AllowUntil", dt_util.as_utc(until))]
    assert answer["added"] is True and answer["replaced_lock"] is False
    assert abs(dt_util.parse_datetime(answer["until"]) - until) < timedelta(seconds=1)
    coordinator.xbox_family.async_create_override.assert_not_called()
    coordinator.async_request_refresh.assert_awaited()


async def test_the_target_is_the_name_microsoft_lists(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    fake = _serve(
        coordinator,
        FakeOverrides(_state(name="Desktop"), _state(("AllowUntil", _iso(until)), name="Desktop")),
    )
    await coordinator.async_add_time_bonus(CHILD, "Windows", until)
    assert fake.posts[0][1] == "Desktop"


async def test_windows_extra_time_over_a_lock_is_refused(coordinator):
    fake = _serve(coordinator, FakeOverrides(_state(("BlockUntil", FAR))))
    with pytest.raises(HomeAssistantError, match="replace_lock"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30)
        )
    assert fake.posts == []


async def test_an_unknown_override_type_counts_as_a_lock(coordinator):
    fake = _serve(coordinator, FakeOverrides(_state(("SomethingNew", FAR))))
    with pytest.raises(HomeAssistantError, match="replace_lock"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30)
        )
    assert fake.posts == []


async def test_windows_extra_time_with_replace_lock_replaces_it(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    coordinator._platform_override_until[(CHILD, "windows")] = until + timedelta(hours=2)
    fake = _serve(
        coordinator,
        FakeOverrides(_state(("BlockUntil", FAR)), _state(("AllowUntil", _iso(until)))),
    )
    answer = await coordinator.async_add_time_bonus(CHILD, "Windows", until, replace_lock=True)
    assert answer["replaced_lock"] is True
    assert fake.types == ["AllowUntil"]
    assert (CHILD, "windows") not in coordinator._platform_override_until


async def test_an_expired_lock_does_not_need_replace_lock(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    _serve(
        coordinator,
        FakeOverrides(
            _state(("BlockUntil", "2020-01-01T00:00:00.000+0000")),
            _state(("AllowUntil", _iso(until))),
        ),
    )
    answer = await coordinator.async_add_time_bonus(CHILD, "Windows", until)
    assert answer["replaced_lock"] is False


async def test_not_confirmed_cancels_what_was_written(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    # Microsoft stored something, but not extra time ending when asked.
    fake = _serve(coordinator, FakeOverrides(_state(), _state(("AllowUntil", FAR))))
    with pytest.raises(
        HomeAssistantError, match="Microsoft did not confirm extra time on Windows"
    ) as err:
        await coordinator.async_add_time_bonus(CHILD, "Windows", until)
    assert fake.types == ["AllowUntil", "Cancel"]
    assert fake.posts[1][1] == "Windows"
    assert "has been cancelled" in str(err.value)


async def test_not_confirmed_with_nothing_listed_sends_no_cancel(coordinator):
    fake = _serve(coordinator, FakeOverrides(_state(), _state()))
    fake.post_error = FamilySafetyWebAPIError("API request failed with status 400: bad type")
    with pytest.raises(HomeAssistantError, match="did not confirm extra time on Windows") as err:
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30)
        )
    assert fake.types == ["AllowUntil"]
    assert "status 400" in str(err.value) and "Nothing is in force" in str(err.value)


async def test_not_confirmed_keeps_extra_time_given_before(coordinator):
    earlier = _state(("AllowUntil", FAR))
    fake = _serve(coordinator, FakeOverrides(earlier, earlier))
    with pytest.raises(HomeAssistantError, match="left as it was"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30)
        )
    assert fake.types == ["AllowUntil"]


async def test_not_confirmed_never_cancels_a_lock(coordinator):
    lock = _state(("BlockUntil", FAR))
    fake = _serve(coordinator, FakeOverrides(lock, lock))
    with pytest.raises(HomeAssistantError, match="nothing was cancelled"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30), replace_lock=True
        )
    assert fake.types == ["AllowUntil"]


async def test_a_failed_read_back_cancels_when_no_lock_was_in_force(coordinator):
    fake = _serve(
        coordinator, FakeOverrides(_state(), FamilySafetyWebAPIError("status 503"))
    )
    with pytest.raises(HomeAssistantError, match="did not confirm extra time on Windows") as err:
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30)
        )
    assert fake.types == ["AllowUntil", "Cancel"]
    assert "read-back failed" in str(err.value)


async def test_a_failed_read_back_over_a_lock_cancels_nothing(coordinator):
    fake = _serve(
        coordinator,
        FakeOverrides(_state(("BlockUntil", FAR)), FamilySafetyWebAPIError("status 503")),
    )
    with pytest.raises(HomeAssistantError, match="state is unknown"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30), replace_lock=True
        )
    assert fake.types == ["AllowUntil"]


async def test_a_failed_cleanup_is_reported(coordinator):
    fake = _serve(coordinator, FakeOverrides(_state(), _state(("AllowUntil", FAR))))
    fake.cancel_error = FamilySafetyWebAPIError("status 500")
    with pytest.raises(HomeAssistantError, match="failed as well"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=30)
        )


async def test_a_write_error_with_the_extra_time_listed_succeeds(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    fake = _serve(coordinator, FakeOverrides(_state(), _state(("AllowUntil", _iso(until)))))
    fake.post_error = FamilySafetyWebAPIError("status 500: aggregator error")
    answer = await coordinator.async_add_time_bonus(CHILD, "Windows", until)
    assert answer["added"] is True and fake.types == ["AllowUntil"]


async def test_extra_time_listed_beside_a_lock_fails_and_keeps_the_lock(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    fake = _serve(
        coordinator,
        FakeOverrides(
            _state(("BlockUntil", FAR)),
            _state(("BlockUntil", FAR), ("AllowUntil", _iso(until))),
        ),
    )
    with pytest.raises(HomeAssistantError, match="still locked"):
        await coordinator.async_add_time_bonus(CHILD, "Windows", until, replace_lock=True)
    assert fake.types == ["AllowUntil"]


async def test_add_fails_when_the_overrides_cannot_be_read(coordinator):
    fake = _serve(coordinator, FakeOverrides(FamilySafetyWebAPIError("status 503")))
    with pytest.raises(HomeAssistantError, match="no extra time was given"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=5)
        )
    assert fake.posts == []


async def test_add_fails_when_windows_is_not_listed(coordinator):
    fake = _serve(coordinator, FakeOverrides({"lockablePlatforms": []}))
    with pytest.raises(HomeAssistantError, match="no Windows device limits"):
        await coordinator.async_add_time_bonus(
            CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=5)
        )
    assert fake.posts == []


async def test_an_unknown_child_is_refused(coordinator):
    with pytest.raises(ValueError):
        await coordinator.async_add_time_bonus(
            "999", "Windows", dt_util.utcnow() + timedelta(minutes=5)
        )


# --- cancel -------------------------------------------------------------------------
async def test_cancelling_windows_extra_time(coordinator):
    fake = _serve(coordinator, FakeOverrides(_state(("AllowUntil", FAR)), _state()))
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Windows")
    assert answer == {"cancelled": True, "lock_in_force": False}
    assert fake.types == ["Cancel"] and fake.posts[0][1] == "Windows"


async def test_cancel_sends_nothing_while_windows_is_locked(coordinator):
    fake = _serve(
        coordinator, FakeOverrides(_state(("BlockUntil", FAR), ("AllowUntil", FAR)))
    )
    with pytest.raises(HomeAssistantError, match="locked"):
        await coordinator.async_cancel_time_bonus(CHILD, "Windows")
    assert fake.posts == []


async def test_cancel_with_no_extra_time_sends_nothing(coordinator):
    for state in (
        _state(),
        _state(("AllowUntil", "2020-01-01T00:00:00.000+0000")),
        {"lockablePlatforms": []},
    ):
        fake = _serve(coordinator, FakeOverrides(state))
        answer = await coordinator.async_cancel_time_bonus(CHILD, "Windows")
        assert answer == {"cancelled": False, "reason": "no_extra_time"}
        assert fake.posts == []


async def test_cancel_fails_when_the_extra_time_stays(coordinator):
    still = _state(("AllowUntil", FAR))
    _serve(coordinator, FakeOverrides(still, still))
    with pytest.raises(HomeAssistantError, match="left the Windows extra time"):
        await coordinator.async_cancel_time_bonus(CHILD, "Windows")


async def test_cancel_reports_a_lock_listed_afterwards(coordinator):
    _serve(coordinator, FakeOverrides(_state(("AllowUntil", FAR)), _state(("BlockUntil", FAR))))
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Windows")
    assert answer == {"cancelled": True, "lock_in_force": True}


async def test_cancel_fails_when_the_result_cannot_be_read(coordinator):
    _serve(
        coordinator,
        FakeOverrides(_state(("AllowUntil", FAR)), FamilySafetyWebAPIError("status 503")),
    )
    with pytest.raises(HomeAssistantError, match="could not be read back"):
        await coordinator.async_cancel_time_bonus(CHILD, "Windows")


async def test_cancel_fails_when_the_overrides_cannot_be_read(coordinator):
    fake = _serve(coordinator, FakeOverrides(FamilySafetyWebAPIError("status 503")))
    with pytest.raises(HomeAssistantError, match="nothing was cancelled"):
        await coordinator.async_cancel_time_bonus(CHILD, "Windows")
    assert fake.posts == []


# --- mobile API and lock state ---------------------------------------------------------
async def test_mobile_override_requests():
    api = FamilySafetyWebAPI.__new__(FamilySafetyWebAPI)
    api._request = AsyncMock(return_value={"lockablePlatforms": []})
    until = dt_util.parse_datetime("2026-01-02T03:04:05+01:00")

    await api.set_mobile_device_override(CHILD, "Windows", "AllowUntil", until)
    method, path = api._request.await_args.args
    kwargs = api._request.await_args.kwargs
    assert (method, path) == ("POST", f"/v4/devicelimits/{CHILD}/overrides")
    assert kwargs["json_data"] == {
        "target": "Windows",
        "overrideType": "AllowUntil",
        "validUntil": "2026-01-02T02:04:05.000+0000",
        "culture": "en-US",
    }
    assert kwargs["extra_headers"] == {"Plat-Info": "ALL"}

    assert await api.get_mobile_device_overrides(CHILD) == {"lockablePlatforms": []}
    assert api._request.await_args.args == ("GET", f"/v4/devicelimits/{CHILD}/overrides")

    api._request.return_value = None
    with pytest.raises(FamilySafetyWebAPIError):
        await api.get_mobile_device_overrides(CHILD)


def test_extra_time_is_not_read_as_a_lock():
    """The /v4 overrides read (mobile_overrides) never counts AllowUntil as a lock."""
    from custom_components.microsoft_family_safety.mobile_overrides import (
        mobile_blocked_platforms,
    )

    state = {
        "lockablePlatforms": [
            {"appliesTo": "Windows", "overrides": [{"overrideType": "AllowUntil"}], "devices": []},
            {"appliesTo": "Xbox", "overrides": [{"overrideType": "BlockUntil"}], "devices": []},
        ]
    }
    assert mobile_blocked_platforms(state) == ["Xbox"]
