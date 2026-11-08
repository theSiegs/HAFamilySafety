"""The Xbox through the Xbox Family Settings API (xapprove), with mocked HTTP.

Lock = an override of type 2 (block) until the lock's end; unlock = type 100
(cancel), only for our own block; extra time = type 1 (allow). An allow and a
block never stand together: ours is cancelled first. A parent's override is
never replaced. A refused token falls back to the web route.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety import xapprove
from custom_components.microsoft_family_safety import xbox_token_probe as probe
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_XAPPROVE,
    CONF_XBOX_XUIDS,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import FamilySafetyDataUpdateCoordinator
from custom_components.microsoft_family_safety.screen_time import iso_utc, parse_utc
from custom_components.microsoft_family_safety.sensor import FamilySafetyXboxOverrideSensor
from custom_components.microsoft_family_safety.xbox_xapprove import parse_xuid_option

from .test_platform_lock import CHILD, FakeWebAPI, _make

STORED = "stored-family-refresh-SECRET"
NEW_RT = "new-xbox-refresh-SECRET"
ACCESS = "xbox-access-SECRET"
USER_TOKEN = "user-token-SECRET"
XSTS_TOKEN = "xsts-token-SECRET"
PARENT_XUID = "2535400000000001"
CHILD_XUID = "2535400000000002"
GAMERTAG = "AlexPlaysGames"
XA_ACCESS = "xapprove-access-SECRET"
XA_USER_TOKEN = "xapprove-user-SECRET"
OTHER_XSTS = "other-xsts-SECRET"
FAMILY_ACCESS = "family-access-SECRET"
SECRETS = (
    STORED, NEW_RT, ACCESS, USER_TOKEN, XSTS_TOKEN, PARENT_XUID, CHILD_XUID, GAMERTAG,
    XA_ACCESS, XA_USER_TOKEN, OTHER_XSTS, FAMILY_ACCESS,
)
BASE_AUTH = f"XBL3.0 x=4242;{XSTS_TOKEN}"

PATCH_SESSION = "custom_components.microsoft_family_safety.xbox_xapprove.async_get_clientsession"


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, headers: dict | None = None) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class XboxServer:
    """Login, Xbox Live auth, the Xbox family and xapprove, holding one override."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        #: (overrideType, validUntil) in force, or None.
        self.override: tuple[int, str] | None = None
        self.creates: list[dict] = []
        self.xapprove_status = 200
        self.xsts_status = 200
        self.family_status = 200
        self.create_ignored = False
        #: Which request headers xapprove accepts (the live 401 says: maybe not ours).
        self.accepts = lambda headers: headers["Authorization"] == BASE_AUTH
        #: login.live.com for the xapprove scope: 200 or an error status.
        self.xapprove_scope_status = 400
        #: XSTS relying party -> status, for the ones other than http://xboxlive.com.
        self.rp_status: dict[str, int] = {}
        self.scopes: list[str] = []
        #: Authorization values the Xbox family read accepts.
        self.family_auth = {BASE_AUTH}

    def state(self) -> dict:
        overrides = []
        if self.override is not None:
            code, until = self.override
            overrides.append({
                "overrideType": code, "target": 1, "appliesTo": [1], "validUntil": until,
            })
        return {
            "actions": {"create": {"allow": [1], "block": [1]}, "cancel": {"target": [1]}},
            "overrides": overrides,
        }

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if url == probe.TOKEN_URL:
            assert kwargs["data"]["refresh_token"] == STORED
            scope = kwargs["data"]["scope"]
            self.scopes.append(scope)
            if scope == xapprove.XAPPROVE_SCOPE:
                if self.xapprove_scope_status != 200:
                    return FakeResponse(self.xapprove_scope_status, {"error": "invalid_scope"})
                return FakeResponse(200, {
                    "expires_in": 86400, "scope": scope, "access_token": XA_ACCESS,
                    "refresh_token": NEW_RT,
                })
            assert scope == probe.XBOX_SCOPE
            return FakeResponse(200, {
                "token_type": "bearer", "expires_in": 86400, "scope": probe.XBOX_SCOPE,
                "access_token": ACCESS, "refresh_token": NEW_RT,
            })
        if url == probe.USER_AUTH_URL:
            ticket = kwargs["json"]["Properties"]["RpsTicket"].split("=", 1)[1]
            if ticket not in (ACCESS, XA_ACCESS):
                return FakeResponse(400, None, {"x-err": "2148916238"})
            return FakeResponse(200, {
                "Token": USER_TOKEN if ticket == ACCESS else XA_USER_TOKEN,
                "NotAfter": iso_utc(dt_util.utcnow() + timedelta(days=14)),
                "DisplayClaims": {"xui": [{"uhs": "4242"}]},
            })
        if url == probe.XSTS_URL:
            rp = kwargs["json"]["RelyingParty"]
            if rp != "http://xboxlive.com":
                status = self.rp_status.get(rp, 400)
                if status != 200:
                    return FakeResponse(status, {"XErr": 2148916236}, {})
                return FakeResponse(200, {
                    "Token": OTHER_XSTS,
                    "NotAfter": iso_utc(dt_util.utcnow() + timedelta(hours=16)),
                    "DisplayClaims": {"xui": [{"uhs": "4242"}]},
                })
            if self.xsts_status != 200:
                return FakeResponse(self.xsts_status, {"XErr": 2148916233}, {})
            return FakeResponse(200, {
                "Token": XSTS_TOKEN,
                "NotAfter": iso_utc(dt_util.utcnow() + timedelta(hours=16)),
                "DisplayClaims": {"xui": [{"uhs": "4242", "xid": PARENT_XUID, "gtg": "SamParent"}]},
            })
        if url.startswith("https://accounts.xboxlive.com/family/"):
            assert kwargs["headers"]["Authorization"] in self.family_auth
            if self.family_status != 200:
                return FakeResponse(self.family_status, None)
            # As seen live: no isAdult, a role per member.
            return FakeResponse(200, {"familyUsers": [
                {"xuid": PARENT_XUID, "role": "Adult", "firstName": "Sam", "lastName": "Rivera",
                 "gamertag": "SamParent"},
                {"xuid": CHILD_XUID, "role": "Child", "firstName": "Alex", "lastName": "Rivera",
                 "gamertag": GAMERTAG},
            ]})
        if url.startswith(xapprove.XAPPROVE_HOST):
            assert f"/user/{CHILD_XUID}/" in url
            if self.xapprove_status != 200:
                return FakeResponse(self.xapprove_status, None, {"x-err": "2148916238"})
            if not self.accepts(kwargs["headers"]):
                return FakeResponse(
                    401, None, {"WWW-Authenticate": 'XBL3.0 realm="xapprove.xboxlive.com"'}
                )
            if url.endswith("/settings/screentime/overrides/create"):
                assert method == "POST"
                body = kwargs["json"] if "json" in kwargs else json.loads(kwargs["data"])
                assert body["target"] == 1
                self.creates.append(dict(body))
                if not self.create_ignored:
                    if body["overrideType"] == 100:
                        self.override = None
                    else:
                        self.override = (body["overrideType"], body["validUntil"])
                return FakeResponse(200, self.state())
            if url.endswith("/settings/screentime/overrides"):
                assert method == "GET"
                return FakeResponse(200, self.state())
        raise AssertionError(f"unexpected {method} {url}")

    def create_types(self) -> list[int]:
        return [c["overrideType"] for c in self.creates]

    def urls(self, prefix: str) -> list[str]:
        return [u for _, u, _ in self.calls if u.startswith(prefix)]


def _hours(hours: float):
    return dt_util.utcnow() + timedelta(hours=hours)


def _make_rig(hass, options: dict | None = None, web_up: bool = True):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": STORED},
        options={CONF_XBOX_XAPPROVE: True, **(options or {})},
    )
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    coord.data["accounts"][CHILD].update({"first_name": "Alex", "surname": "Rivera"})
    web = FakeWebAPI()
    web.has_web_cookies = web_up
    web.family_context_state = "ready" if web_up else "auth_required"
    web.last_isolated_error_code = None
    coord.web_api = web
    authenticator = SimpleNamespace(
        refresh_token=STORED, access_token=FAMILY_ACCESS, _login_lock=asyncio.Lock(),
        perform_refresh=AsyncMock(),
    )
    coord.api = SimpleNamespace(api=SimpleNamespace(authenticator=authenticator))
    coord._async_persist_current_auth = AsyncMock()
    return coord, entry


@pytest.fixture
def server():
    srv = XboxServer()
    with patch(PATCH_SESSION, return_value=srv):
        yield srv


@pytest.fixture
def rig(hass, server):
    coord, _ = _make_rig(hass)
    return coord


def _no_secrets(value) -> None:
    text = json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text


# Lock and unlock ---------------------------------------------------------------


async def test_lock_sends_a_block_until_the_end(rig, server):
    end = _hours(3)
    result = await rig.async_lock_platform(CHILD, "Xbox", end)

    assert result["lock_path"] == "xapprove"
    assert result["applied"]["type"] == "blockUntil"
    assert server.create_types() == [2]
    assert parse_utc(server.creates[0]["validUntil"]) == parse_utc(iso_utc(end))
    assert rig.web_api.writes == []  # the web route was not needed
    hold = rig.platform_hold(CHILD, "Xbox")
    assert hold["path"] == "xapprove" and hold["confirmed"] is True
    state = rig.platform_hold_state(CHILD, "Xbox")
    assert state["hold"] == "ours" and state["override"] == "blockUntil"
    assert state["lock_path"] == "xapprove"
    assert "Xbox" in rig.data["accounts"][CHILD]["blocked_platforms"]
    _no_secrets(result)


async def test_a_lock_already_in_force_long_enough_is_not_written_again(rig, server):
    end = _hours(3)
    await rig.async_lock_platform(CHILD, "Xbox", end)
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert server.create_types() == [2]
    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(end))


async def test_a_longer_lock_cancels_ours_first(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(1))
    later = _hours(4)
    await rig.async_lock_platform(CHILD, "Xbox", later)
    assert server.create_types() == [2, 100, 2]
    assert parse_utc(server.override[1]) == parse_utc(iso_utc(later))
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "ours"


@pytest.mark.parametrize("code", [1, 2])
async def test_a_parents_override_is_left_alone(rig, server, code):
    server.override = (code, iso_utc(_hours(1)))
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["applied"] == "left_parent_override"
    assert result["current"]["type"] == ("allowUntil" if code == 1 else "blockUntil")
    assert server.creates == []
    assert rig.platform_hold(CHILD, "Xbox") is None
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_unlock_cancels_our_block(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await rig.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"]["type"] == "none"
    assert result["lock_path"] == "xapprove"
    assert server.create_types() == [2, 100]
    assert server.override is None
    assert rig.platform_hold(CHILD, "Xbox") is None
    assert "Xbox" not in rig.data["accounts"][CHILD]["blocked_platforms"]


async def test_unlock_leaves_a_block_that_replaced_ours(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    server.override = (2, iso_utc(_hours(6)))  # a parent paused for longer in the app
    result = await rig.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    assert result["current"]["type"] == "blockUntil"
    assert server.create_types() == [2]
    assert rig.platform_hold(CHILD, "Xbox") is None
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_unlock_after_a_parent_resumed_writes_nothing(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    server.override = None  # "Resume" in the app
    result = await rig.async_unlock_platform(CHILD, "Xbox")
    assert result["applied"] == "left_parent_override"
    assert server.create_types() == [2]


async def test_unlock_without_a_hold_cancels_nothing(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    result = await rig.async_unlock_platform(CHILD, "Xbox")
    assert result["applied"] == "not_locked"
    assert server.creates == []


async def test_the_switch_off_cancels_what_is_in_force(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    await rig.async_release_platform(CHILD, "Xbox")
    assert server.create_types() == [100]


async def test_a_write_that_does_not_read_back_falls_back_to_the_web(rig, server):
    server.create_ignored = True
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["lock_path"] == "web"
    assert [(p, k) for p, k, _ in rig.web_api.writes] == [("xbox", "blockUntil")]


# A refused token ---------------------------------------------------------------


@pytest.mark.parametrize("where", ["xapprove", "xsts"])
async def test_a_refused_token_falls_back_and_raises_a_repair(hass, rig, server, where, caplog):
    caplog.set_level("DEBUG")
    if where == "xapprove":
        server.xapprove_status = 403
    else:
        server.xsts_status = 401
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "web"
    assert [(p, k) for p, k, _ in rig.web_api.writes] == [("xbox", "blockUntil")]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"xapprove_refused_{rig.entry.entry_id}")
    assert issue is not None
    assert not rig._xapprove_usable()
    assert rig.platform_hold(CHILD, "Xbox")["path"] == "web"
    for secret in SECRETS:
        assert secret not in caplog.text

    # Within the 6 hours, xapprove is not asked again.
    calls = len(server.calls)
    await rig.async_unlock_platform(CHILD, "Xbox")
    assert len(server.calls) == calls


async def test_success_clears_the_repair(hass, rig, server):
    server.xapprove_status = 403
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    server.xapprove_status = 200
    rig._xapprove_refused_until = 0
    await rig._async_poll_xapprove(rig.data["accounts"], {}, {})
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xapprove_refused_{rig.entry.entry_id}") is None


# Extra time --------------------------------------------------------------------


async def test_extra_time_is_an_allow(rig, server):
    until = _hours(0.5)
    result = await rig.async_add_time_bonus(CHILD, "Xbox", until)

    assert server.create_types() == [1]
    assert parse_utc(server.creates[0]["validUntil"]) == parse_utc(iso_utc(until))
    assert result["read_back"]["type"] == "allowUntil"
    assert result["released_hold"] is False
    assert result["lock_path"] == "xapprove"
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "bonus_ours"


async def test_extra_time_over_our_lock_cancels_it_first(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))

    assert server.create_types() == [2, 100, 1]
    assert result["released_hold"] is True
    assert rig.platform_hold(CHILD, "Xbox") is None


async def test_a_lock_over_our_extra_time_cancels_it_first(rig, server):
    await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert server.create_types() == [1, 100, 2]
    assert rig._bonuses == {}


async def test_extra_time_over_a_parents_pause_is_refused(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    with pytest.raises(HomeAssistantError, match="parent's Xbox override"):
        await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    assert server.creates == []
    assert rig.web_api.writes == []


async def test_cancel_extra_time(rig, server):
    await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    result = await rig.async_cancel_time_bonus(CHILD, "Xbox")
    assert result["cancelled"] is True and result["ours"] is True
    assert server.create_types() == [1, 100]
    assert rig._bonuses == {}


async def test_cancel_never_touches_a_block(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    result = await rig.async_cancel_time_bonus(CHILD, "Xbox")
    assert result["cancelled"] is False
    assert server.creates == []


# Poll, sensor, tokens, child id ----------------------------------------------


async def test_poll_reads_the_override(hass, rig, server):
    server.override = (2, iso_utc(_hours(2)))
    accounts = rig.data["accounts"]
    overrides: dict = {}
    read_at: dict = {}
    await rig._async_poll_xapprove(accounts, overrides, read_at)

    assert accounts[CHILD]["xbox_xapprove"]["type"] == "blockUntil"
    assert overrides[CHILD]["type"] == "blockUntil" and CHILD in read_at
    assert "Xbox" in accounts[CHILD]["blocked_platforms"]
    sensor = FamilySafetyXboxOverrideSensor(rig, rig.entry, CHILD)
    assert sensor.native_value == "block"
    attrs = sensor.extra_state_attributes
    assert attrs["ours"] is False and attrs["hold"] == "parent" and attrs["source"] == "xapprove"
    _no_secrets(accounts)
    _no_secrets(attrs)


async def test_tokens_are_reused(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    await rig.async_unlock_platform(CHILD, "Xbox")
    assert len(server.urls(probe.TOKEN_URL)) == 1
    assert len(server.urls(probe.XSTS_URL)) == 1
    assert len(server.urls("https://accounts.xboxlive.com/")) == 1


async def test_the_entered_xuid_wins_over_the_lookup(hass, server):
    coord, _ = _make_rig(hass, {CONF_XBOX_XUIDS: f"alex = {CHILD_XUID}"})
    await coord.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert server.urls("https://accounts.xboxlive.com/") == []
    assert server.create_types() == [2]


async def test_no_xuid_falls_back(hass, server):
    server.family_status = 404
    coord, _ = _make_rig(hass)
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["lock_path"] == "web"
    assert coord._xapprove_usable()  # not a refusal


def test_parse_xuid_option():
    assert parse_xuid_option("Alex=2533274800000000, 1234567890123456: 2533274800000001\nbad") == {
        "alex": "2533274800000000", "1234567890123456": "2533274800000001",
    }


def test_matching_by_name_id_and_elimination():
    members = [
        {"xuid": "1", "isAdult": True, "firstName": "Sam"},
        {"xuid": "2", "firstName": "Alex", "lastName": "Rivera"},
        {"xuid": "3", "firstName": "Robin", "lastName": "Doe", "msaUserId": "0xNOPE"},
        {"xuid": "4", "puid": "000000000000000A"},
    ]
    children = {
        "10": {"first_name": "Someone", "surname": "Else"},
        "20": {"first_name": "Alex", "surname": "Rivera"},
        "30": {"first_name": "Jamie", "surname": "Lee"},
    }
    found = xapprove.match_children(members, children)
    assert found["10"] == ("4", "family_id")  # hex puid 0xA == 10
    assert found["20"] == ("2", "family_name")
    assert found["30"] == ("3", "elimination")


# probe_xapprove ----------------------------------------------------------------


async def test_probe_refuses_without_confirm_risk(rig, server):
    with pytest.raises(ServiceValidationError, match="confirm_risk"):
        await rig.async_probe_xapprove(False)
    assert server.calls == []


async def test_probe_answers_without_secrets(rig, server, caplog):
    caplog.set_level("DEBUG")
    server.override = (2, iso_utc(_hours(2)))
    result = await rig.async_probe_xapprove(True)

    assert result["steps"]["msa_refresh"]["status"] == 200
    assert result["steps"]["msa_refresh"]["refresh_token_discarded"] is True
    assert result["steps"]["xsts_authorize"]["display_claims_xui_keys"] == ["gtg", "uhs", "xid"]
    assert result["stored_refresh_token_unchanged"] is True
    assert result["parent_xuid_claim_present"] is True
    family = result["family"]
    assert family["ok"] is True and family["members"] == 2
    assert family["adults"] == 1 and family["roles"] == ["Adult", "Child"]
    assert family["signed_in_parent_listed"] is True
    assert result["working_method"] == "xbl3" and result["method_saved"] is True
    assert result["overrides_attempts"] == [
        {"name": "xbl3", "stage": "xapprove", "status": 200, "xerr": None}
    ]
    assert result["children"] == {CHILD: {"resolved": True, "via": "family_name"}}
    read = result["overrides_read"]
    assert read["ok"] is True and read["override_active"] is True
    assert read["type"] == "blockUntil" and read["raw_type"] == 2
    assert read["actions"] == {"create": {"allow": [1], "block": [1]}, "cancel": {"target": [1]}}
    assert result["family_refresh_after"] == {"ok": True}
    assert server.creates == []  # the probe writes nothing
    _no_secrets(result)
    for secret in SECRETS:
        assert secret not in caplog.text


async def test_probe_reports_a_refusal(rig, server):
    server.xapprove_status = 401
    result = await rig.async_probe_xapprove(True)
    read = result["overrides_read"]
    assert read["ok"] is False and read["status"] == 401 and read["xerr"] == "2148916238"
    assert result["working_method"] is None and result["method_saved"] is False
    _no_secrets(result)


async def test_probe_service_is_for_administrators(hass, rig, hass_read_only_user, hass_admin_user):
    hass.data.setdefault(DOMAIN, {})[rig.entry.entry_id] = rig
    _register_services(hass)
    rig.async_probe_xapprove = AsyncMock(return_value={"steps": {}})
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "probe_xapprove", {"confirm_risk": True}, blocking=True,
            return_response=True, context=Context(user_id=hass_read_only_user.id),
        )
    response = await hass.services.async_call(
        DOMAIN, "probe_xapprove", {"confirm_risk": True}, blocking=True,
        return_response=True, context=Context(user_id=hass_admin_user.id),
    )
    assert response == {"steps": {}}
    rig.async_probe_xapprove.assert_awaited_once_with(True)


async def test_the_first_poll_finds_the_child_before_data_is_set(rig, server):
    accounts = rig.data["accounts"]
    rig.data = None  # the first poll: nothing stored yet
    server.override = (1, iso_utc(_hours(1)))
    await rig._async_poll_xapprove(accounts, {}, {})
    assert accounts[CHILD]["xbox_xapprove"]["type"] == "allowUntil"



# probe_xapprove: other ways of signing the request -------------------------------


async def test_probe_tries_each_way_until_one_works_and_the_integration_uses_it(
    hass, rig, server, hass_storage
):
    server.accepts = lambda h: (
        h["Authorization"] == BASE_AUTH and h.get("x-xbl-contract-version") == "2"
    )
    result = await rig.async_probe_xapprove(True)

    names = [a["name"] for a in result["overrides_attempts"]]
    assert names == ["xbl3", "xbl3_contract_1", "xbl3_contract_2"]
    first = result["overrides_attempts"][0]
    assert first["status"] == 401
    assert first["www_authenticate"] == {
        "name": "WWW-Authenticate", "value": 'XBL3.0 realm="xapprove.xboxlive.com"',
    }
    assert result["working_method"] == "xbl3_contract_2"
    assert result["overrides_read"]["ok"] is True
    assert result["overrides_read"]["method"] == "xbl3_contract_2"
    _no_secrets(result)

    # The integration now signs xapprove that way, also after a restart.
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert server.create_types() == [2]
    restarted = FamilySafetyDataUpdateCoordinator(hass, rig.entry)
    await restarted._async_load_xapprove_method()
    assert restarted._xapprove_method.name == "xbl3_contract_2"
    assert restarted._xapprove_client().method.contract == "2"


async def test_probe_reaches_the_xapprove_scope_token(rig, server):
    server.xapprove_status = 200
    server.xapprove_scope_status = 200
    server.accepts = lambda h: h["Authorization"] == f"WLID1.0 t={XA_ACCESS}"
    result = await rig.async_probe_xapprove(True)

    assert result["working_method"] == "wlid_t_xapprove_scope"
    msa = result["steps"]["msa_refresh[service::xapprove.xboxlive.com::MBI_SSL]"]
    assert msa["status"] == 200 and msa["refresh_token_discarded"] is True
    assert result["stored_refresh_token_unchanged"] is True
    tried = {a["name"]: a for a in result["overrides_attempts"]}
    assert tried["xbl3_rp_http_xapprove"]["stage"] == "token"
    assert tried["xbl3_rp_http_xapprove"]["step"] == "xsts_authorize"
    _no_secrets(result)


async def test_probe_tries_everything_when_nothing_works(rig, server, caplog):
    caplog.set_level("DEBUG")
    server.accepts = lambda h: False
    result = await rig.async_probe_xapprove(True)

    assert [a["name"] for a in result["overrides_attempts"]] == [
        m.name for m in xapprove.PROBE_METHODS if m.scheme not in xapprove.SISU_SCHEMES
    ]
    assert result["identity"] == "family_safety_signin" and result["xbox_signed_in"] is False
    tried = {a["name"]: a for a in result["overrides_attempts"]}
    assert tried["wlid_t_xapprove_scope"] == {
        "name": "wlid_t_xapprove_scope", "stage": "token", "step": "msa_refresh",
        "status": 400, "xerr": None, "error": "invalid_scope",
    }
    assert tried["msauth_family_token"]["status"] == 401
    assert tried["xbl3_family_token_ticket"]["step"] == "user_authenticate"
    assert result["working_method"] is None and result["method_saved"] is False
    # Each scope is asked of login.live.com once, however many ways use it.
    assert server.scopes == [probe.XBOX_SCOPE, xapprove.XAPPROVE_SCOPE]
    assert len([u for u in server.urls(probe.XSTS_URL)]) == 1 + 2 + 1  # base, 2 RPs, rp://
    assert rig._xapprove_method.name == "xbl3"
    assert result["stored_refresh_token_unchanged"] is True
    assert result["family_refresh_after"] == {"ok": True}
    assert server.creates == []
    _no_secrets(result)
    for secret in SECRETS:
        assert secret not in caplog.text


def test_adults_by_role():
    assert xapprove.is_adult({"role": "Adult"}) is True
    assert xapprove.is_adult({"role": "FamilyOrganizer"}) is True
    assert xapprove.is_adult({"role": "Child"}) is False
    assert xapprove.is_adult({"memberRole": "Teen"}) is False
    assert xapprove.is_adult({"isAdult": False, "role": "Adult"}) is False
    assert xapprove.is_adult({}) is False
    members = [
        {"xuid": "1", "role": "Adult", "firstName": "Sam", "lastName": "Rivera"},
        {"xuid": "5", "firstName": "Sam", "lastName": "Rivera"},  # the signed-in parent, no role
        {"xuid": "2", "role": "Child", "firstName": "Alex", "lastName": "Rivera"},
    ]
    children = {
        "20": {"first_name": "Alex", "surname": "Rivera"},
        "30": {"first_name": "Robin", "surname": "Rivera"},
    }
    found = xapprove.match_children(members, children, {"5"})
    assert found == {"20": ("2", "family_name")}  # Robin is not paired with an adult
