"""Microsoft Family Safety API client.

Requests that pyfamilysafety does not cover, sent to the same mobile
aggregator the Family Safety app uses, on the single refresh-token chain of
the pyfamilysafety authenticator. Nothing here talks to account.microsoft.com:
the Family web session (browser cookies, Family dashboard) is not used at
runtime, so its daily interactive sign-in (#52) no longer concerns the
integration. Xbox settings go through ``xbox_family_api`` instead.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import json
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from homeassistant.util import dt as dt_util

from ._pyfamilysafety_compat import (
    authenticator_access_token_expired,
    authenticator_authorization_header,
)
from .const import DAY_KEYS
from .mobile_overrides import MOBILE_OVERRIDES_PATH, MOBILE_OVERRIDES_PLAT_INFO

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://mobileaggregator.family.microsoft.com/api"
_APP_VERSION = "v 1.26.0.1001"
_USER_AGENT = f"Family Safety-prod/({_APP_VERSION}) Android/33 google/Pixel 4 XL"


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse Microsoft's ISO-8601 timestamps to an aware UTC datetime.

    They arrive as '2026-09-20T22:59:59.461Z'.  fromisoformat() accepts the
    trailing 'Z' from Python 3.11, but this module is the one place a bad
    timestamp would silently read as "not blocked", so be explicit and
    forgiving rather than clever.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: Any) -> Any:
    """Give the activity report's naive UTC times an explicit offset (#65)."""
    parsed = _parse_iso_utc(value) if value else None
    return parsed.isoformat() if parsed else value


#: Sites and searches kept in the sensor attributes (the raw report is ~30 KB).
_WEB_ACTIVITY_LIMIT = 50


def summarize_web_activity(web: Any, search: Any) -> dict[str, Any]:
    """Trim the mobile web and search activity reports for a sensor.

    ``web`` is None when Microsoft has no report (403): the summary then says
    so instead of pretending there was no browsing.
    """
    available = isinstance(web, dict)
    web = web if available else {}
    search = search if isinstance(search, dict) else {}
    sites = []
    for status in ("allowed", "blocked"):
        for entry in web.get(status) or []:
            if isinstance(entry, dict) and entry.get("domain"):
                sites.append({
                    "domain": entry["domain"],
                    "visits": int(entry.get("totalCountOfVisitsToDomain") or 0),
                    "last_visit": _utc_iso(entry.get("lastVisitToDomain")),
                    "blocked": status == "blocked",
                })
    sites.sort(key=lambda s: s["visits"], reverse=True)
    # Blocked attempts per URL (#65): the URL tells whether a block is a false
    # positive. Allowed URLs are not listed, there are far too many of them.
    blocked_urls = []
    for entry in web.get("blocked") or []:
        if not isinstance(entry, dict):
            continue
        for visit in entry.get("visits") or []:
            page = visit.get("pageVisit") if isinstance(visit, dict) else None
            if isinstance(page, dict) and page.get("url"):
                blocked_urls.append({
                    "url": page["url"],
                    "visits": int(visit.get("visitCount") or 1),
                    "last_visit": visit.get("date"),
                    "domain": entry.get("domain"),
                })
    blocked_urls.sort(key=lambda b: b["last_visit"] or "", reverse=True)
    for row in blocked_urls:
        row["last_visit"] = _utc_iso(row["last_visit"])
    # One row per URL variant of a term: grouped by term like the Family
    # dashboard, which shows the highest visitCount and the latest date (#65).
    terms: dict[str, dict[str, Any]] = {}
    for group in search.get("searched") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("searches") or []:
            if not isinstance(item, dict):
                continue
            term = item.get("search")
            if isinstance(term, dict):
                term = term.get("searchTerm")
            if not isinstance(term, str) or not term.strip():
                continue
            row = {
                "search": term,
                "visits": int(item.get("visitCount") or 1),
                "date": item.get("date"),
                "site": group.get("domain"),
            }
            prev = terms.get(term)
            if prev is None:
                terms[term] = row
                continue
            prev["visits"] = max(prev["visits"], row["visits"])
            if (row["date"] or "") > (prev["date"] or ""):
                prev["date"], prev["site"] = row["date"], row["site"]
    searches = sorted(terms.values(), key=lambda s: s["date"] or "", reverse=True)
    for row in searches:
        row["date"] = _utc_iso(row["date"])
    return {
        "report_available": available,
        "total_visits": sum(s["visits"] for s in sites),
        "site_count": len(sites),
        "blocked_count": sum(1 for s in sites if s["blocked"]),
        "sites": sites[:_WEB_ACTIVITY_LIMIT],
        "searches": searches[:_WEB_ACTIVITY_LIMIT],
        "blocked_sites": blocked_urls[:_WEB_ACTIVITY_LIMIT],
    }


class FamilySafetyWebAPI:
    """Mobile aggregator requests beyond pyfamilysafety (schedules, lists, limits)."""

    def __init__(
        self, authenticator, session: aiohttp.ClientSession | None = None
    ) -> None:
        self._authenticator = authenticator
        #: Home Assistant's shared session (async_get_clientsession) for the
        #: mobile API, which needs no cookies. Without one (tests, scripts) a
        #: private session is created and closed by close().
        self._shared_session = session
        self._session: aiohttp.ClientSession | None = None
        #: Outcome of the last Windows schedule read, for the Connection sensor.
        self.screentime_policy_status: str = "unknown"
        #: child id -> (time.monotonic(), payload) of the last schedules GET.
        self._mobile_schedule_cache: dict[str, tuple[float, dict]] = {}
        #: Schedule writes (Windows and Xbox) go one at a time. Each is a GET
        #: then a PATCH of the same schedules document: several sent at once
        #: (one per day, e.g. setting a whole week) mostly fail with dropped
        #: connections, and an interleaved GET could send back a stale day.
        self._mobile_schedule_write_lock = asyncio.Lock()

    #: Per-request timeout of the mobile API (the shared session has none).
    _MOBILE_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

    async def _ensure_session(self) -> None:
        if self._shared_session is not None and not self._shared_session.closed:
            self._session = self._shared_session
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._MOBILE_TIMEOUT)

    async def _ensure_auth(self) -> None:
        """Use the single pyfamilysafety authenticator for mobile API auth.

        Keeping exactly one refresh-token chain prevents two independent token
        grants from rotating the same Microsoft refresh token out from under each
        other.
        """
        refresh_token = getattr(self._authenticator, "refresh_token", None)
        if not refresh_token:
            raise FamilySafetyWebAPIError("No refresh token available")
        try:
            if authenticator_access_token_expired(self._authenticator):
                await self._authenticator.perform_refresh()
        except Exception as err:
            raise FamilySafetyWebAPIError(f"Mobile token refresh failed: {err}") from err

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": authenticator_authorization_header(self._authenticator),
            "User-Agent": _USER_AGENT,
            "X-Requested-With": "com.microsoft.familysafety",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict | list | None:
        """Make an authenticated request to the mobile API."""
        await self._ensure_session()
        await self._ensure_auth()
        assert self._session is not None
        url = f"{_BASE_URL}{path}"
        headers = self._build_headers()
        if extra_headers:
            headers.update(extra_headers)

        async def do_request() -> dict | list | None:
            async with self._session.request(
                method, url, headers=headers, json=json_data, params=params,
                timeout=self._MOBILE_TIMEOUT,
            ) as resp:
                return await self._handle_response(resp)

        try:
            try:
                return await do_request()
            except FamilySafetyWebAPIError as err:
                if "status 401" not in str(err) and "status 403" not in str(err):
                    raise
                await self._authenticator.perform_refresh()
                headers.update(self._build_headers())
                return await do_request()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FamilySafetyWebAPIError(
                f"Mobile API request failed: {err!r}", transient=True
            ) from err

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict | list | None:
        if resp.status in (200, 201, 204):
            text = await resp.text()
            if not text:
                return None
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return None
        text = await resp.text()
        raise FamilySafetyWebAPIError(
            f"API request failed with status {resp.status}: {text[:200]}",
            status=resp.status,
        )

    async def get_web_browsing_settings(self, child_id: str) -> dict | None:
        """Web filter settings with the blocked and allowed sites.

        Without ``exceptions=true`` the sites come back as an empty list (the
        Family Safety app always asks for them, #64). ``blockedSites`` and
        ``allowedSites`` are derived from them for the Web Filter sensor.
        """
        result = await self._request(
            "GET", f"/v1/WebRestrictions/{child_id}", params={"exceptions": "true"}
        )
        if not isinstance(result, dict):
            return None
        exceptions = result.get("exceptions")
        if isinstance(exceptions, list):
            sites = [e for e in exceptions if isinstance(e, dict) and e.get("website")]
            result.setdefault("blockedSites", [e["website"] for e in sites if not e.get("allowed")])
            result.setdefault("allowedSites", [e["website"] for e in sites if e.get("allowed")])
        return result

    async def get_screentime_policy(self, child_id: str) -> dict | None:
        """The child's Windows schedule from the mobile API, or None.

        Never raises: the outcome is kept in ``screentime_policy_status`` for
        the Connection sensor ("ok", "no_policy_data", "network_error" or
        "error").
        """
        try:
            policy = await self.get_mobile_screentime_policy(child_id)
        except FamilySafetyWebAPIError as err:
            self.screentime_policy_status = "network_error" if err.transient else "error"
            _LOGGER.debug("Mobile screen-time schedule read failed: %s", err)
            return None
        except Exception as err:  # noqa: BLE001 - a malformed answer must not stop the poll
            self.screentime_policy_status = "error"
            _LOGGER.debug("Mobile screen-time schedule read failed: %r", err)
            return None
        self.screentime_policy_status = "ok" if policy is not None else "no_policy_data"
        return policy

    @staticmethod
    def parse_block_until(payload: object) -> datetime | None:
        """Pull an active blockUntil expiry out of an overrides payload.

        Shape is from the captured POST *response*:
            {"data":{"screenTimeOverrides":[
                {"screenTimeOverrideType":"blockUntil","validUntil":"…Z"}]}}

        Deliberately tolerant: returns None on anything unrecognised, which
        callers read as "unknown", never as "not blocked".
        """
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        container = data if isinstance(data, dict) else payload
        overrides = container.get("screenTimeOverrides")
        if not isinstance(overrides, list):
            return None
        for entry in overrides:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("screenTimeOverrideType", "")).lower() != "blockuntil":
                continue
            raw = entry.get("validUntil")
            if not isinstance(raw, str):
                continue
            parsed = _parse_iso_utc(raw)
            if parsed is not None:
                return parsed
        return None

    async def get_content_settings(self, child_id: str) -> dict | None:
        result = await self._request("GET", f"/v1/ContentRestrictions/{child_id}")
        return result if isinstance(result, dict) else None

    async def get_devices(self, child_id: str) -> dict | None:
        result = await self._request("GET", f"/v1/devices/{child_id}")
        return result if isinstance(result, dict) else None

    async def get_web_restrictions(self, child_id: str) -> dict:
        """The child's web restrictions from the mobile API, with the site lists.

        ``exceptions=true`` is what makes Microsoft include the blocked and
        allowed websites (``exceptions``: ``{"website", "allowed"}``); without
        it the list comes back empty. Raises when Microsoft answers no data.
        """
        result = await self._request(
            "GET", f"/v1/WebRestrictions/{child_id}", params={"exceptions": "true"}
        )
        if not isinstance(result, dict):
            raise FamilySafetyWebAPIError("WebRestrictions read returned no data")
        return result

    async def get_mobile_device_overrides(self, child_id: str) -> dict:
        """The child's device-limits overrides from the mobile API, read fresh.

        See mobile_overrides for the route and the answer's shape.
        """
        result = await self._request(
            "GET",
            MOBILE_OVERRIDES_PATH.format(child_id=child_id),
            extra_headers={"Plat-Info": MOBILE_OVERRIDES_PLAT_INFO},
        )
        if not isinstance(result, dict):
            raise FamilySafetyWebAPIError("The mobile overrides answer was not an object")
        return result

    async def set_mobile_device_override(
        self,
        child_id: str,
        target: str,
        override_type: str,
        valid_until: datetime,
        culture: str = "en-US",
    ) -> dict | list | None:
        """POST one device-limits override on the mobile API; return the answer.

        ``target`` is the platform's ``appliesTo`` name from a read.
        """
        body = {
            "target": target,
            "overrideType": override_type,
            "validUntil": dt_util.as_utc(valid_until).strftime("%Y-%m-%dT%H:%M:%S.000%z"),
            "culture": culture,
        }
        _LOGGER.debug("Mobile device-limits override for %s: %s", child_id, body)
        return await self._request(
            "POST",
            MOBILE_OVERRIDES_PATH.format(child_id=child_id),
            json_data=body,
            extra_headers={"Plat-Info": MOBILE_OVERRIDES_PLAT_INFO},
        )

    async def mobile_raw_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        *,
        params: dict[str, str] | None = None,
        encoded: bool = False,
    ) -> dict[str, Any]:
        """Diagnostic: one mobile API request, answered as {status, body}; never raises.

        ``encoded``: the path is sent exactly as given (already percent-encoded).
        """
        try:
            await self._ensure_session()
            await self._ensure_auth()
            headers = self._build_headers()
            if extra_headers:
                headers.update(extra_headers)
            url: str | URL = f"{_BASE_URL}{path}"
            if encoded:
                url = URL(url, encoded=True)
            async with self._session.request(
                method, url, headers=headers, json=json_data, params=params,
                timeout=self._MOBILE_TIMEOUT,
            ) as resp:
                text = await resp.text()
                try:
                    body: Any = json.loads(text) if text else None
                except ValueError:
                    body = text
                return {"status": resp.status, "body": body}
        except Exception as err:  # noqa: BLE001 - reported, not raised
            return {"status": None, "error": f"{type(err).__name__}: {err}"}

    # ------------------------------------------------------------------
    # Mobile schedule API (/v4/devicelimits/schedules), as used by the
    # Family Safety iOS app 2.1.0. GET needs ``culture`` and ``time`` (local
    # ISO time with offset); both calls need Plat-Info. Allowances are
    # milliseconds, intervals are {begin, end} "HH:MM:SS".
    # ------------------------------------------------------------------

    @staticmethod
    def _mobile_now() -> str:
        # Home Assistant's time zone, not the container's (which may be UTC).
        return dt_util.now().isoformat(timespec="seconds")

    async def _get_mobile_schedules(self, child_id: str, *, max_age: float = 0.0) -> dict:
        """GET the child's schedules (Windows and Xbox in one payload).

        ``max_age`` lets a read reuse a payload fetched moments ago in the same
        poll (the Xbox read right after the Windows one). Writes always pass 0:
        they rebuild the day from what Microsoft holds right now.
        """
        key = str(child_id)
        cached = self._mobile_schedule_cache.get(key)
        if max_age > 0 and cached and time.monotonic() - cached[0] <= max_age:
            return cached[1]
        result = await self._request(
            "GET",
            f"/v4/devicelimits/schedules/{child_id}",
            params={"culture": "en-us", "time": self._mobile_now()},
            extra_headers={"Plat-Info": "Windows"},
        )
        if not isinstance(result, dict):
            raise FamilySafetyWebAPIError("Mobile schedule response was empty")
        self._mobile_schedule_cache[key] = (time.monotonic(), result)
        return result

    @staticmethod
    def _mobile_platform_schedule(payload: dict, platform: str) -> dict | None:
        for schedule in payload.get("schedules") or []:
            if isinstance(schedule, dict) and str(schedule.get("appliesTo")).lower() == platform.lower():
                return schedule
        return None

    @staticmethod
    def _time_to_slot(value: str, *, ceil: bool) -> int:
        # .NET TimeSpan: midnight at the end of the day is "1.00:00:00".
        text = str(value).strip()
        days = 0
        if "." in text.split(":")[0]:
            day_part, text = text.split(".", 1)
            days = int(day_part or 0)
        parts = [int(float(p)) for p in text.split(":")[:3]] + [0, 0]
        minutes = days * 1440 + parts[0] * 60 + parts[1] + (1 if parts[2] else 0)
        slot = minutes // 30 + (1 if ceil and minutes % 30 else 0)
        return max(0, min(48, slot))

    @classmethod
    def _intervals_to_slots(cls, intervals: list) -> list[bool]:
        slots = [False] * 48
        for item in intervals or []:
            if not isinstance(item, dict):
                continue
            start = cls._time_to_slot(item.get("begin", "00:00:00"), ceil=False)
            end = cls._time_to_slot(item.get("end", "00:00:00"), ceil=True)
            if end <= start:
                end = 48
            for index in range(start, end):
                slots[index] = True
        return slots

    @staticmethod
    def _slots_to_intervals(slots: list[bool]) -> list[dict[str, str]]:
        def fmt(slot: int) -> str:
            if slot >= 48:
                return "1.00:00:00"
            return f"{slot // 2:02d}:{30 if slot % 2 else 0:02d}:00"

        intervals: list[dict[str, str]] = []
        start: int | None = None
        for index, allowed in enumerate(list(slots) + [False]):
            if allowed and start is None:
                start = index
            elif not allowed and start is not None:
                intervals.append({"begin": fmt(start), "end": fmt(index)})
                start = None
        return intervals

    async def get_web_activity(self, child_id: str, days: int = 7) -> dict[str, Any]:
        """Websites visited and Bing searches over the last ``days`` (#65).

        Microsoft answers 403 when the child has no Edge browsing data; that
        is reported as an empty summary with ``report_available`` false.
        """
        end = dt_util.utcnow()
        params = {
            "beginTime": (end - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        path = f"/v1/activityreport/webactivity/{child_id}"
        try:
            web = await self._request("GET", path, params={**params, "allowStatus": "true"})
        except FamilySafetyWebAPIError as err:
            if "status 403" not in str(err):
                raise
            return summarize_web_activity(None, None)
        try:
            search = await self._request(
                "GET", f"/v1/activityreport/searchactivity/{child_id}", params=params
            )
        except FamilySafetyWebAPIError as err:
            _LOGGER.debug("Could not read search activity: %s", err)
            search = None
        return summarize_web_activity(web, search)

    async def get_mobile_screentime_policy(self, child_id: str, platform: str = "Windows") -> dict | None:
        """Return the child's schedule in the shape the entities read (allowance "HH:MM:SS")."""
        schedule = self._mobile_platform_schedule(
            await self._get_mobile_schedules(child_id), platform
        )
        return self._convert_mobile_schedule(schedule) if schedule else None

    @classmethod
    def _convert_mobile_schedule(cls, schedule: dict) -> dict | None:
        daily: dict[str, dict[str, Any]] = {}
        for day, data in (schedule.get("dailyRestrictions") or {}).items():
            if not isinstance(data, dict):
                continue
            seconds = int(data.get("allowance") or 0) // 1000
            intervals = data.get("allottedIntervals") or []
            slots = cls._intervals_to_slots(intervals)
            daily[day] = {
                "allowance": f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}",
                "allottedIntervals": intervals,
                "allowedIntervals": slots,
                # The lock/unlock restore reads 48 booleans only from "timeline".
                "timeline": slots,
            }
        if not daily:
            return None
        return {"isEnabled": schedule.get("enabled"), "appliesTo": schedule.get("appliesTo"),
                "dailyRestrictions": daily}

    async def get_mobile_xbox_screentime_policy(self, child_id: str) -> dict | None:
        """The child's Xbox schedule, from the same mobile payload as Windows.

        Accounts in "PerDeviceType" mode have a separate Xbox schedule, which
        is what the console enforces. It comes back in the Windows GET, so a
        payload fetched moments ago in the same poll is reused.
        """
        payload = await self._get_mobile_schedules(child_id, max_age=30.0)
        schedule = self._mobile_platform_schedule(payload, "Xbox")
        if schedule is None:
            return None
        return self._convert_mobile_schedule(schedule)

    async def get_mobile_day(
        self, child_id: str, day_of_week: int, platform: str = "Windows"
    ) -> dict[str, Any]:
        """One day of a platform's schedule as Microsoft holds it right now.

        Returns ``{"limit": minutes, "allowed_hours": 48 half-hour slots}``.
        Never served from the poll cache: the caller compares it with what it
        wrote. Leaves ``mobile_schedule_ok`` alone (that flag is the poll's).
        """
        payload = await self._get_mobile_schedules(child_id)
        schedule = self._mobile_platform_schedule(payload, platform)
        if schedule is None:
            raise FamilySafetyWebAPIError(f"No {platform} schedule in the mobile response")
        day = (schedule.get("dailyRestrictions") or {}).get(DAY_KEYS[day_of_week])
        if not isinstance(day, dict):
            raise FamilySafetyWebAPIError(
                f"No {DAY_KEYS[day_of_week]} in the {platform} schedule"
            )
        return {
            "limit": int(day.get("allowance") or 0) // 60_000,
            "allowed_hours": self._intervals_to_slots(day.get("allottedIntervals") or []),
        }

    async def set_mobile_xbox_screentime_allowance(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> bool:
        await self._patch_mobile_day(
            child_id,
            day_of_week,
            allowance_ms=(int(hours) * 60 + int(minutes)) * 60_000,
            platform="Xbox",
        )
        return True

    async def _patch_mobile_day(
        self,
        child_id: str,
        day_of_week: int,
        *,
        allowance_ms: int | None = None,
        intervals: list[dict[str, str]] | None = None,
        platform: str = "Windows",
    ) -> None:
        async with self._mobile_schedule_write_lock:
            await self._patch_mobile_day_unlocked(
                child_id, day_of_week, allowance_ms=allowance_ms,
                intervals=intervals, platform=platform,
            )

    async def _patch_mobile_day_unlocked(
        self,
        child_id: str,
        day_of_week: int,
        *,
        allowance_ms: int | None = None,
        intervals: list[dict[str, str]] | None = None,
        platform: str = "Windows",
    ) -> None:
        payload = await self._get_mobile_schedules(child_id)
        schedule = self._mobile_platform_schedule(payload, platform)
        if schedule is None:
            raise FamilySafetyWebAPIError(f"No {platform} schedule in the mobile response")
        day = DAY_KEYS[day_of_week]
        current = (schedule.get("dailyRestrictions") or {}).get(day) or {}
        body = {
            "dailyRestrictions": {
                day: {
                    "allowance": int(current.get("allowance") or 0) if allowance_ms is None else allowance_ms,
                    "allottedIntervals": (
                        (current.get("allottedIntervals") or []) if intervals is None else intervals
                    ),
                }
            },
            "appliesTo": schedule.get("appliesTo") or platform,
            "culture": "en-us",
            "mode": payload.get("mode") or "PerDeviceType",
            "time": self._mobile_now(),
        }
        await self._request(
            "PATCH", f"/v4/devicelimits/schedules/{child_id}",
            json_data=body, extra_headers={"Plat-Info": "Windows"},
        )

    async def set_mobile_schedule_enabled(
        self, child_id: str, enabled: bool, platform: str = "Windows"
    ) -> bool:
        """Turn the platform's weekly schedule on or off, keeping its days intact."""
        async with self._mobile_schedule_write_lock:
            payload = await self._get_mobile_schedules(child_id)
            await self._request(
                "PATCH", f"/v4/devicelimits/schedules/{child_id}",
                json_data={
                    "enabled": bool(enabled),
                    "appliesTo": platform,
                    "culture": "en-us",
                    "mode": payload.get("mode") or "PerDeviceType",
                    "time": self._mobile_now(),
                },
                extra_headers={"Plat-Info": "Windows"},
            )
        return True

    async def set_mobile_screentime_allowance(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> bool:
        await self._patch_mobile_day(
            child_id, day_of_week, allowance_ms=(int(hours) * 60 + int(minutes)) * 60_000
        )
        return True

    async def set_mobile_screentime_intervals(
        self, child_id: str, day_of_week: int, allowed_intervals: list[bool]
    ) -> bool:
        if len(allowed_intervals) != 48:
            raise ValueError("allowed_intervals must contain exactly 48 values")
        await self._patch_mobile_day(
            child_id, day_of_week, intervals=self._slots_to_intervals(allowed_intervals)
        )
        return True

    # App time limits (mobile aggregator, v3/AppLimits/policies), as the
    # Family Safety app sends them since 2026-10 (capture shared on #64):
    # days at the top level, allowance in milliseconds, intervals as
    # {"start", "end"} in 12-hour .NET format ("07:00:00 AM"), whole day =
    # "12:00:00 AM" to "12:00:00 AM" with intervalsEnabled false. Removing a
    # limit is the same PATCH without the day keys.
    _APP_LIMIT_PLATFORMS = {"windows": "Windows", "xbox": "Xbox", "mobile": "Mobile"}

    @staticmethod
    def _to_12h(value: str) -> str:
        parts = [int(p) for p in str(value).strip().split(":")[:3]] + [0, 0]
        hours = parts[0] % 24
        suffix = "AM" if hours < 12 else "PM"
        hours12 = hours % 12 or 12
        return f"{hours12:02d}:{parts[1]:02d}:{parts[2]:02d} {suffix}"

    @staticmethod
    def _duration_to_ms(value: str) -> int:
        parts = [int(p) for p in str(value).strip().split(":")[:3]] + [0, 0]
        return ((parts[0] * 60 + parts[1]) * 60 + parts[2]) * 1000

    def _app_limit_base(self, app_id: str, display_name: str) -> dict[str, Any]:
        return {
            "appId": app_id,
            "displayName": display_name or app_id,
            "enabled": True,
            "blockState": "NotBlocked",
            "appTimeEnforcementPolicy": "Custom",
            "alternateAppIds": [],
        }

    async def get_app_limits_enabled(self, child_id: str, platform: str = "Windows") -> bool | None:
        """Read the per-child "App, feature, and game limits" switch.

        While it is off, no per-app limit is enforced even though the limits
        stay stored. GET v2/AppLimits/settings/{child} with Plat-Info answers
        {"enabled": bool, "lastModified": ...}.
        """
        result = await self._request(
            "GET",
            f"/v2/AppLimits/settings/{child_id}",
            extra_headers={"Plat-Info": platform},
        )
        if isinstance(result, dict) and isinstance(result.get("enabled"), bool):
            return result["enabled"]
        return None

    async def set_app_limits_enabled(
        self, child_id: str, enabled: bool, platform: str = "Windows"
    ) -> bool:
        """Turn the per-child app limits switch on or off (204 on success)."""
        await self._request(
            "PATCH",
            f"/v2/AppLimits/settings/{child_id}",
            json_data={"enabled": bool(enabled)},
            extra_headers={"Plat-Info": platform},
        )
        return True

    async def set_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
        allowance: str,
        start_time: str = "07:00:00",
        end_time: str = "22:00:00",
    ) -> dict | None:
        """Set the same daily limit (and allowed window) on every day."""
        start = self._to_12h(start_time)
        end = self._to_12h(end_time)
        whole_day = start == end
        day = {
            "allowance": self._duration_to_ms(allowance),
            "intervalsEnabled": not whole_day,
            "intervals": [{"start": start, "end": end}],
        }
        body = {**self._app_limit_base(app_id, display_name), **{d: day for d in DAY_KEYS}}
        result = await self._request(
            "PATCH",
            f"/v3/AppLimits/policies/{child_id}/{quote(str(app_id), safe=':')}",
            json_data=body,
            extra_headers={
                "Plat-Info": self._APP_LIMIT_PLATFORMS.get(str(platform).lower(), "Windows")
            },
        )
        return result if isinstance(result, dict) else {}

    async def remove_app_time_limit(
        self, child_id: str, app_id: str, display_name: str, platform: str
    ) -> dict | None:
        """Remove the app's time limit: the same PATCH without the day keys."""
        result = await self._request(
            "PATCH",
            f"/v3/AppLimits/policies/{child_id}/{quote(str(app_id), safe=':')}",
            json_data=self._app_limit_base(app_id, display_name),
            extra_headers={
                "Plat-Info": self._APP_LIMIT_PLATFORMS.get(str(platform).lower(), "Windows")
            },
        )
        return result if isinstance(result, dict) else {}

    async def get_app_activity(
        self, child_id: str, platform: str, days: int = 1
    ) -> list[dict[str, Any]]:
        """The app activity report's entries for one platform, last ``days`` days.

        The per-app policy cannot be read directly: every GET on
        v1-v4/AppLimits/policies answers 405. This report is the one source
        of it: each entry is {appId, displayName, iconUrl, usage, policy,
        blockState, isLegacyBlocked}, ``policy`` null for an app without a
        limit, and every app is listed, used in the range or not. Plat-Info
        is required (400 "The deviceInfo field is required" without it);
        times are UTC. A range of 7 days or more answers 400
        "BeginEndTimeRangeError".
        """
        days = max(1, min(int(days), 6))
        end = datetime.now(timezone.utc).replace(microsecond=0)
        begin = end - timedelta(days=days)
        result = await self._request(
            "GET",
            f"/v4/activityReport/appUsage/{child_id}",
            params={
                "beginTime": begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            extra_headers={
                "Plat-Info": self._APP_LIMIT_PLATFORMS.get(str(platform).lower(), "Windows")
            },
        )
        entries = result.get("appActivity") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            raise FamilySafetyWebAPIError("App activity report without appActivity")
        return [entry for entry in entries if isinstance(entry, dict)]

    async def write_app_policy(
        self, child_id: str, app_id: str, platform: str, body: dict[str, Any]
    ) -> dict | None:
        """PATCH an app policy as given (app_pause.py builds the body)."""
        result = await self._request(
            "PATCH",
            f"/v3/AppLimits/policies/{child_id}/{quote(str(app_id), safe=':')}",
            json_data=body,
            extra_headers={
                "Plat-Info": self._APP_LIMIT_PLATFORMS.get(str(platform).lower(), "Windows")
            },
        )
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _website_url(website: str) -> str:
        """Microsoft wants an absolute URL in the exception list (#54).

        A bare host such as ``example.org`` answers
        ``Settings.WebRestrictions.BadUrlFormat``; ``https://example.org`` is
        accepted and the app shows it as ``example.org``.
        """
        site = str(website).strip()
        if not site.lower().startswith(("http://", "https://")):
            site = f"https://{site}"
        return site

    async def block_website(self, child_id: str, website: str) -> dict | None:
        """Add a website to the blocked list.

        Since 2026-10 the mobile aggregator takes one operation per site:
        ``{"op": "Add", "path": "/exceptions", "value": {"website": url,
        "allowed": false}}``; ``allowed: true`` puts it on the allowed list
        instead. The write answers 204 and the Family Safety app shows the
        site right away. Read it back with get_web_restrictions: the mobile
        ``GET /v1/WebRestrictions`` lists the sites only when asked with
        ``?exceptions=true`` (without it, ``exceptions`` is always empty).
        Confirmed on a real account on 2026-10-27.
        """
        result = await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"operations": [{
                "op": "Add",
                "path": "/exceptions",
                "value": {"website": self._website_url(website), "allowed": False},
            }]},
        )
        return result if isinstance(result, dict) else {}

    async def allow_website(self, child_id: str, website: str) -> dict | None:
        """Add a website to the allowed list (see block_website)."""
        result = await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"operations": [{
                "op": "Add",
                "path": "/exceptions",
                "value": {"website": self._website_url(website), "allowed": True},
            }]},
        )
        return result if isinstance(result, dict) else {}

    async def remove_website(self, child_id: str, website: str) -> dict | None:
        """Remove a website from the blocked or allowed list.

        The removal takes the URL as a plain string value: an object there
        makes the aggregator answer 500 ``JsonReaderException``.
        """
        result = await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"operations": [{
                "op": "Remove",
                "path": "/exceptions",
                "value": self._website_url(website),
            }]},
        )
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _patch_operations(path: str, value: Any) -> dict[str, Any]:
        """Body of a mobile-aggregator PATCH since 2026-10 (issue #54).

        WebRestrictions and ContentRestrictions now take a JSON-Patch-like
        document, ``{"operations": [{"op": "Replace", "path": "/field",
        "value": ...}]}``. The op is spelled with a capital letter: ``replace``
        is rejected with a generic "Invalid Payload". Confirmed on 2026-10-27
        with a real change of ``maxAgeRating`` read back from Microsoft.
        """
        return {"operations": [{"op": "Replace", "path": path, "value": value}]}

    async def toggle_web_filter(self, child_id: str, enabled: bool) -> dict | None:
        result = await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data=self._patch_operations("/enabled", bool(enabled)),
        )
        return result if isinstance(result, dict) else None

    async def set_age_rating(self, child_id: str, age: int) -> dict | None:
        if not 3 <= age <= 21:
            raise ValueError("age must be between 3 and 21")
        result = await self._request(
            "PATCH",
            f"/v1/ContentRestrictions/{child_id}",
            json_data=self._patch_operations("/maxAgeRating", int(age)),
        )
        return result if isinstance(result, dict) else None

    async def set_acquisition_policy(
        self, child_id: str, require_approval: bool
    ) -> dict | None:
        # Same document shape as set_age_rating; the field itself has not
        # been exercised against a real account yet.
        result = await self._request(
            "PATCH",
            f"/v1/ContentRestrictions/{child_id}",
            json_data=self._patch_operations(
                "/acquisitionPolicy", "freeOnly" if require_approval else "unrestricted"
            ),
        )
        return result if isinstance(result, dict) else None

    async def close(self) -> None:
        if (
            self._session
            and self._session is not self._shared_session
            and not self._session.closed
        ):
            await self._session.close()
        self._session = None


class FamilySafetyWebAPIError(Exception):
    """Exception raised for API errors.

    ``transient`` marks a network failure or timeout (Microsoft did not
    answer), as opposed to an answer that refused the request.
    """

    def __init__(
        self, message: str = "", status: int | None = None, *, transient: bool = False
    ) -> None:
        super().__init__(message)
        #: The HTTP status Microsoft answered, when it answered.
        self.status = status
        self.transient = transient
