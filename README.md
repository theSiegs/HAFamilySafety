# <img src="https://raw.githubusercontent.com/noiwid/HAFamilySafety/main/custom_components/microsoft_family_safety/brand/icon.png" alt="Microsoft Family Safety" width="30" > Microsoft Family Safety for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![Marketplace][marketplace-shield]][marketplace]
[![License][license-shield]][license]
[![Buy Me A Beer](https://img.shields.io/badge/Buy%20me%20a%20beer-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/noiwid)

A full-featured Home Assistant custom integration for **Microsoft Family Safety**. Monitor screen time, manage app restrictions, lock accounts, control web filtering, and adjust daily limits — all from your Home Assistant dashboard.

Authentication is **native**: you sign in to Microsoft from Home Assistant itself. The Playwright auth add-on is no longer required and is kept only as a [legacy fallback](#legacy-add-on-mode).

**Supported platforms:** Windows, Xbox, Mobile

> **Domain:** `microsoft_family_safety` | **IoT Class:** Cloud Polling | **Languages:** English, French, German

![Dashboard Example](https://raw.githubusercontent.com/noiwid/HAFamilySafety/main/examples/dashboard.png)

---

## Disclaimer

This integration uses **unofficial, undocumented APIs** for Microsoft Family Safety. It is not approved, endorsed, or supported by Microsoft. Microsoft may modify or disable the underlying APIs at any time. Use at your own risk and in compliance with Microsoft's terms of service.

---

## Breaking changes

> **If you are upgrading from a version that used the Playwright auth add-on, read this before you upgrade.**

### Entity IDs change when you re-add the integration

Native authentication requires a **new config entry**. When you remove the old integration entry and add it again, Home Assistant generates fresh entity IDs, and the new ones include the device name as a prefix:

| Before | After |
|--------|-------|
| `number.firstname_sunday_limit` | `number.firstname_lastname_family_safety_firstname_sunday_limit` |
| `switch.firstname_lock` | `switch.firstname_family_safety_firstname_lock` |
| `button.firstname_approve_request` | `button.firstname_family_safety_firstname_approve_request` |

This **breaks dashboards, automations, scripts and templates** that reference the old IDs. After migrating:

1. Go to **Settings > Devices & Services > Entities** and check for entities marked *restored / unavailable* — those are the old IDs.
2. Either rename the new entities back to the old IDs (**Entities > entity > Settings > Entity ID**), which is the fastest way to keep existing YAML working, or update every reference.
3. Search your config for the old IDs:
   ```bash
   grep -rn "switch\.<name>_lock\|number\.<name>_.*_limit\|time\.<name>_" config/
   ```
   Don't forget **dashboard YAML**, **automations**, **scripts**, **scenes** and **template sensors**. The example dashboard in [`examples/dashboard.yaml`](examples/dashboard.yaml) already uses the prefixed IDs.

> Note the device-name prefix is not identical across platforms: sensors, numbers and time entities are prefixed with *first name + surname*, while switches and buttons are prefixed with the *first name only*.

### The add-on is no longer required

The Playwright auth add-on is now a **legacy fallback**. New installations should not install it. Existing installations keep working unchanged — see [Legacy add-on mode](#legacy-add-on-mode).

---

## Architecture

The integration authenticates natively inside Home Assistant and talks to both Microsoft backends itself. **No add-on, no browser automation, and no separate container are needed in the normal case.**

### The two Microsoft APIs

Microsoft Family Safety has **two distinct APIs**, each with its own authentication. The integration uses both:

| API | Base URL | Auth Method | Capabilities |
|-----|----------|-------------|-------------|
| **Mobile API** | `mobileaggregator.family.microsoft.com` | OAuth Bearer token (from a long-lived refresh token) | Almost everything: family roster, devices, usage, screen time schedules (Windows and Xbox), account lock, app blocking and app time limits, websites, age rating, ask-to-buy, requests. |
| **Web API** (private) | `account.microsoft.com/family/api/` | Microsoft session cookies + a `__RequestVerificationToken` antiforgery token | Only a fallback for the schedules, on older entries that still hold a web session. Nothing Xbox. |

The **Xbox** has its own sign-in and API: the lock and extra time go through the **Xbox Family Settings API** (`xapprove.xboxlive.com`) with a separate [Xbox Family Settings sign-in](#sign-in-to-xbox-family-settings), and fall back to today's Xbox schedule on the mobile API. The Family web session, which Microsoft makes some accounts renew with an interactive sign-in every 24 hours ([#52](https://github.com/noiwid/HAFamilySafety/issues/52)), is never used for the Xbox, and new entries do not create one at all (see [The Xbox and the web session](#the-xbox-and-the-web-session)).

### How native authentication works

You sign in **once**, in your normal browser, through a short-lived reverse proxy that Home Assistant mounts inside its own HTTP server.

For a new entry that sign-in is the one the Family Safety app uses: Microsoft's mobile OAuth page, which hands back the refresh token for the mobile API, and that is all. No web session is created or stored.

**Older entries that hold a Family web session** (created with Xbox ticked before the Xbox moved to its own sign-in) re-run two steps behind that single sign-in when they reauthenticate:

**Step 1 — Family web session.** The proxy serves the regular `account.microsoft.com` sign-in. Answer **Yes** to "Stay signed in?": that answer is what makes Microsoft issue persistent session cookies (valid for about a year), so the Family session no longer has to be renewed every few hours. (Before 2.0.4 the flow signed in through the mobile app's OAuth page instead, which never asks the question, and sessions expired after about 7 hours.) Once you are signed in, the same tab is redirected a few times so the Family dashboard can be loaded and its `__RequestVerificationToken` read. This part may take up to about 60 seconds; Home Assistant shows a waiting screen.

**Step 2 — mobile refresh token.** With the cookies from step 1, Home Assistant completes the mobile OAuth authorization **server-side**: Microsoft answers with a single redirect carrying the authorization code, with no second sign-in page. That code becomes the refresh token used for the mobile API. Should Microsoft ever require an interactive step here, the flow falls back to finishing it in your browser.

The browser part of step 1 is required and cannot be removed: a cold-start, purely server-side bootstrap is impossible because Microsoft gates the Family dashboard behind an interactive `prompt=none` OAuth hop.

### After sign-in: no browser at all

After the sign-in, Home Assistant talks to both APIs **directly over HTTPS** from the integration. The schedules go through the mobile API (`/v4/devicelimits/schedules`, the endpoint the Family Safety app uses), with the web API's `/family/api/st` as a fallback when a web session exists.

### Data flow

```
Poll loop (default every 5 min)
  ├─ Mobile API   → roster, devices, apps, usage, schedules (Windows + Xbox),
  │                 websites, content restrictions, app limits switch
  └─ xapprove     → each child's Xbox override (Xbox Family Settings sign-in)

Service call / entity write
  ├─ Mobile API   → schedules, account lock, app blocking and app limits,
  │                 websites, age rating, ask-to-buy, requests, Xbox limits,
  │                 the Xbox lock's fallback (today's Xbox schedule)
  └─ xapprove     → Xbox lock, unlock, extra time
```

### Legacy add-on mode

The `familysafety-playwright/` add-on is being phased out. Since 2.0.22 a new install can no longer choose it (the add-on URL and key fields are gone from the setup and options forms). Existing add-on entries keep working as before, and reauthenticating one (from the notification, Repairs or **Reconfigure**) moves it to the native sign-in for good: its add-on URL and key are cleared and the add-on can then be uninstalled.

The cases the add-on used to cover are handled natively now: an HTTP-only instance can sign in through the manual redirect step (the mobile token is all a new entry needs), and the schedules no longer depend on a browser session at all.

Legacy and native mode are mutually exclusive per config entry; there is no automatic downgrade from native to add-on at runtime.

---

## Features

| Category | What you can do |
|----------|----------------|
| **Native authentication** | Sign in to Microsoft directly from Home Assistant — no add-on, no container, no copy-pasted redirect URL |
| **Account Lock** | Lock/unlock a child's entire account with a single switch |
| **Screen time monitoring** | Track daily usage per child and per device |
| **Screen time policies** | Adjust daily allowances and allowed time intervals per day — directly from the UI |
| **App management** | Block/unblock apps, set per-app time limits and windows |
| **Web filtering** | Block/unblock domains, toggle content filtering, set PEGI age ratings |
| **Purchase controls** | Enable/disable ask-to-buy via service call |
| **Request handling** | Approve or deny pending screen time requests from HA |
| **Connection diagnostics** | A dedicated sensor reports whether the mobile API and the web session are healthy |
| **Optimistic updates** | UI values update instantly when changing limits or intervals (reverts on failure) |

---

## Installation

### Prerequisites

- **Home Assistant** 2024.1.0 or newer, running on any installation type (OS, Supervised, Container, Core)
- A Home Assistant URL reachable from your browser over **HTTPS**. If your instance is HTTP-only on the local network, see [`allow_insecure_http_auth`](#insecure-local-http-authentication)
- A **Microsoft parent account** with at least one child in the Family Safety group

> No add-on and no Docker container are required.

### Step 1 — Install the Integration

#### Via the Marketplace (Recommended)

1. Go to **Settings > Marketplace > Browse**
2. Search for **Microsoft Family Safety** and install it
3. Restart Home Assistant

#### Manual Installation

1. Download the latest release from [GitHub Releases](https://github.com/noiwid/HAFamilySafety/releases)
2. Copy the `custom_components/microsoft_family_safety` folder into your `config/custom_components/` directory
3. Restart Home Assistant

### Step 2 — Configure the Integration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Microsoft Family Safety**
3. Set the **update interval** and the **monitored platforms** (Windows, Mobile: the lock switches). For the Xbox, sign in to Xbox Family Settings afterwards under **Configure** (see [below](#sign-in-to-xbox-family-settings)). Click **Submit**
4. Home Assistant shows an **Open website** button. Click it: a browser window opens on the Microsoft sign-in page, served through Home Assistant's temporary authentication proxy
5. Sign in with your **Microsoft parent account** (the family organizer, not a child account) and complete MFA if prompted
6. The window finishes as soon as Microsoft hands back the token. (An older entry that still holds a Family web session, when it reauthenticates, also completes that session in the background; that can take up to about a minute: keep the window open.)
7. Home Assistant shows **Microsoft Family Safety sign-in completed**. Click **Continue**
8. The integration discovers all child accounts and devices automatically. Screen-time entities can read `unknown` for up to one update interval (5 minutes by default) while the first data pull completes; that is expected

> The old flow — copy an auth URL, sign in, paste the redirect URL back — is gone in the normal case. It survives as a fallback on HTTP-only instances and for legacy add-on users.

### Integration Options

**Settings > Devices & Services > Microsoft Family Safety > Configure**:

| Option | Range / Format | Default | Notes |
|--------|----------------|---------|-------|
| Update interval (seconds) | 30 – 3600 | 300 (5 min) | |
| Monitored platforms | Windows / Mobile | Windows | One lock switch per platform. The Xbox lock switch comes with the Xbox Family Settings sign-in, see [below](#the-xbox-and-the-web-session) |
| Users allowed to control Microsoft Family Safety | list of Home Assistant users | *(empty: everybody)* | See [Who can control the integration](#who-can-control-the-integration) |
| Strict mode | on / off | off | Places a `lock_platform` lock again when it is cancelled elsewhere; see [Strict mode](#strict-mode) |
| Xbox lock and extra time through the Xbox Family Settings API (xapprove) | on / off | off | With the Xbox Family Settings sign-in: the Xbox lock switch and Override sensor, Xbox lock, unlock and extra time; see [below](#xbox-through-the-xbox-family-settings-api-xapprove) |
| Xbox user ids | `child=xuid, ...` | *(empty: looked up)* | Only if the automatic lookup fails; see [below](#xbox-through-the-xbox-family-settings-api-xapprove) |
| Sign in to Xbox Family Settings (next page) / Sign out of Xbox Family Settings | tick and submit | off | Opens the Xbox sign-in page, then checks it and offers the xapprove option; sign out is shown only while signed in. The page's top line says whether you are signed in; see [below](#sign-in-to-xbox-family-settings) |
| Allow insecure local HTTP authentication (testing only) | on / off | off | See below |

#### Strict mode

When on, a Windows or Xbox (xapprove) lock placed by `microsoft_family_safety.lock_platform` that Microsoft reports as cancelled before its end (released in the Family Safety app, for example) is placed again on the next update, at most every 10 minutes, and `microsoft_family_safety_lock_reasserted` is fired. `unlock_platform` ends it.

**Keep strict mode off when another integration manages the locks**, such as Hearth: it would lock again right after a parent's own unlock in the Family Safety app and fight the parent's decision. Such an integration should read the `hold` attribute instead (`ours`, `parent` or `none`, on the Allowed Now binary sensors and the platform lock switches) and adopt the parent's change.

#### Who can control the integration

Home Assistant lets every user press any button, flip any switch or change any number, unless an entity permission policy says otherwise, and Home Assistant has no screen to write such policies. If your child has a Home Assistant account, they could otherwise lift their own limits from the integration's entities.

The option **Users allowed to control Microsoft Family Safety** closes that gap:

- **Empty** (default): nothing changes, every user can use the buttons and settings.
- **Some users ticked**: only they and the administrators can. Any other account is refused, with a warning in the log, on every button, switch, daily limit and time window of the integration, and on every `microsoft_family_safety.*` action.
- Automations and scripts started by Home Assistant itself are never affected. A script started by a person from the dashboard runs with that person's rights.

Give the child a **non-administrator** account and tick the parents: an administrator can change this option, so a child with an administrator account bypasses it. The child still sees the entities (Home Assistant cannot hide them per user) but can no longer use them. Changing the option takes effect immediately, without reloading the integration.

#### The Xbox and the web session

The Xbox **never uses the Microsoft Family web session**. Its lock and extra time go through the Xbox Family Settings API with the separate [Xbox Family Settings sign-in](#sign-in-to-xbox-family-settings) (next sections), with today's Xbox schedule as the lock's fallback; its daily limits (the Xbox Screen Time Limits switch and the `number.{prefix}_xbox_{day}_limit` entities) run on the mobile token. So the web session's daily sign-in on some accounts ([#52](https://github.com/noiwid/HAFamilySafety/issues/52)) no longer matters for the Xbox, and nothing asks for a sign-in on the Xbox's behalf.

- **New entries** never create a web session: the sign-in is the short one of the Family Safety app.
- **Older entries** that hold one (created with Xbox ticked) keep it, for the schedules' fallback only. Xbox is no longer offered among the monitored platforms; ticking it used to mean "use the web session for the Xbox".
- **Gone with the web route:** Xbox play time (the web activity report): the Xbox Screen Time and Xbox Time Remaining sensors are no longer created (remove the old ones from the entity list if you like), and the Xbox Allowed Now sensor reports `allowed` only from the schedule and the override in force, with `used_minutes` unknown. The web Xbox lock (blockUntil on `account.microsoft.com`) is replaced by xapprove.

The **Xbox lock switch** and the **Xbox Override** sensor appear for each child once the Xbox Family Settings sign-in is made, the xapprove option is on, and the child's Xbox has been read once.

#### The Xbox lock's fallback: today's Xbox schedule

When xapprove cannot be used (no Xbox sign-in, no Xbox user id for the child, the API out of reach, a write it does not confirm, or Microsoft refusing the token), `lock_platform` on Xbox (and the Xbox lock switch):

1. reads **today's** Xbox schedule day exactly as Microsoft holds it (allowance and allowed hours, and whether the Xbox schedule is on) and saves it with the lock, on disk, before writing anything;
2. sets today's Xbox allowance to **0** through the mobile token (the allowed hours stay as they are; a schedule that was off is turned on for the lock and off again afterwards);
3. puts the saved day back at `unlock_platform` (or the switch turned off), at the end of the lock, and **at midnight in any case**: such a lock never lasts past midnight and is never carried to the next day. A restart that missed the moment puts it back on the first update.

The day is only put back while it still shows the lock: when a parent changed it in the Family Safety app meanwhile, the parent's day is kept and `unlock_platform` answers `left_parent_override`. A new `lock_platform` on that child then answers `left_parent_override` too, without writing over the parent's day. If the saved day cannot be put back once the lock has ended, it is retried at every update and a Repairs issue ("An Xbox schedule was not put back") stays up until it succeeds. The answers of `lock_platform` and `unlock_platform` and the attributes of the lock switch and Allowed Now sensor carry `lock_path`: `xapprove` or `mobile_schedule`.

While such a lock holds, the switch reads locked, `hold` is `ours` with `override: blockUntil` until the lock's end, and `allowance_minutes` shows the saved allowance, not the 0 written; the Xbox daily limit number for today shows 0. It needs a separate Xbox schedule (Family Safety's "per device type" limits); otherwise the lock fails with a clear error and nothing is changed. Check once that your console stops when today's Xbox daily limit is set to no time mid-day. Xbox extra time (`add_time_bonus`, `cancel_time_bonus`) has no fallback: without xapprove it fails with a clear error.

#### Xbox through the Xbox Family Settings API (xapprove)

The Xbox Family Settings app pauses a child's Xbox and gives extra time through its own API, `https://xapprove.xboxlive.com`, signed with an Xbox Live token instead of the Family web session. With **Xbox lock and extra time through the Xbox Family Settings API (xapprove)** on (off by default), the integration does the same.

xapprove only takes the token the Xbox Family Settings app itself gets: tried live, it answered `401 token_required` to every token the Family Safety sign-in can produce. So it needs the separate [Xbox Family Settings sign-in](#sign-in-to-xbox-family-settings) below; the option does nothing without it. It works the same whether or not the entry holds a Family web session.

What it does, for the Xbox only (Windows is unchanged):

| Call | Request | When |
|------|---------|------|
| `lock_platform` (and the Xbox lock switch on) | `overrideType` 2, block until the lock's end (`until`, or `duration_hours` as before; Hearth sends midnight) | Nothing in force, or only an override of ours. A lock of ours already in force until then or later is kept, with no write. |
| `unlock_platform` | `overrideType` 100, cancel | Only when the block in force is ours (its end within 2 minutes of the one we sent). |
| Xbox lock switch off | `overrideType` 100, cancel | Whatever is in force, as before for the switch. |
| `add_time_bonus` | `overrideType` 1, allow until `now + minutes` (or `until`) | Nothing in force, or only an override of ours. |
| `cancel_time_bonus` | `overrideType` 100, cancel | Only extra time (an allow) in force; a block is never touched. |

Each write goes to `target` 1 (screen time on all of the child's Xbox consoles), one at a time per child, and is believed only when the reply's `overrides` show it; otherwise the call falls back (below).

**An allow and a block never stand together.** The Xbox Family Settings app always cancels before it switches between extra time and a pause, so when an override of **ours** is in force the integration sends a cancel first, then the new override. It does so also when the new one has the same type (a longer lock): cancelling first is the order the app itself uses, and the server accepting a create on top of an override is not verified.

**A parent's override is left alone.** An override that is not ours (a pause or extra time given in the Xbox Family Settings or Family Safety app, or one that replaced ours) is never cancelled to put ours in: `lock_platform` answers `applied: "left_parent_override"` with `current` and writes nothing, `unlock_platform` answers `left_parent_override` too (or `not_locked` when we held no lock), `add_time_bonus` fails with a clear error, and the `hold` attribute reads `parent`. Hearth therefore sees it as a parent's decision. "Ours" means a block ending within 2 minutes of the lock this integration recorded, or an allow ending within 2 minutes of the extra time `add_time_bonus` recorded.

**The answers** keep their shape. `lock_path` (in `lock_platform` and `unlock_platform` answers, the lock switch and the Allowed Now sensor) is `xapprove` or `mobile_schedule` for the Xbox; `add_time_bonus` and `cancel_time_bonus` answers carry `lock_path: xapprove`, and `cancel_time_bonus` adds `ours` (whether the extra time cancelled was ours). Each child with an Xbox gets a sensor **Xbox Override** (`sensor.{prefix}_xbox_override`): `block`, `allow` or `none`, with `valid_until` (local time), `ours`, `hold` (`ours`, `bonus_ours`, `parent`, `none`), `lock_path`, `source` (`xapprove`) and `read_at`. Every update reads each child's override through xapprove.

**The child's Xbox user id.** It is looked up once in the parent's Xbox family (`accounts.xboxlive.com/family/memberXuid(...)`, with the parent's id from the XSTS token): a member whose Microsoft account id matches the child, else the same first and last name as in Family Safety (unique on both sides), else the last child left on both sides. Adults are never matched: a member is an adult by `isAdult`, else by a role naming one (`Adult`, `Parent`, `Organizer`, `Admin`, `Owner`, `Guardian`; a role naming a child, teen or minor wins), and the signed-in parent is always left out. If that does not work for a child (`probe_xapprove` says so), enter **Xbox user ids** in the options as `child=xuid` pairs separated by commas, the child given by first name or account id. The ids are kept in memory and in the options only, never logged, and redacted from diagnostics.

**Fallback.** When xapprove cannot be used for a lock (no Xbox user id for the child, a network error, a reply that does not show the write), the lock goes through [today's Xbox schedule](#the-xbox-locks-fallback-todays-xbox-schedule); extra time fails with a clear error. A lock of ours placed through xapprove that cannot be reached is kept until its end (a new lock ending no later answers it as in force; unlock says to try again later). When Microsoft **refuses the token** (401 or 403 from a token step or from xapprove, after one retry with fresh tokens), xapprove is left alone for 6 hours, a warning is logged once and a Repairs issue "The Xbox Family Settings API refused the Xbox token" says so; it goes away at the first call that works again.

**The risk.** See the sign-in below. Signing in checks xapprove and offers to turn the option on; [`probe_xapprove`](#xbox-family-settings-api-probe-diagnostic) gives the details when it does not work.

#### Sign in to Xbox Family Settings

This is a second Microsoft sign-in, separate from the Family Safety one, made **as the Xbox Family Settings app**: the integration uses that app's public client id (`00000000482C8F49`) and the Xbox Authentication Library's "SISU" flow, the way the open-source XAL implementations do. Use the **parent** Microsoft account, the same one you sign in to Family Safety with, not a child's. The code sign-in works from a phone or a computer (the Home Assistant app included); the fallback needs a computer's browser tools.

1. **Settings > Devices & services > Microsoft Family Safety > Configure**. The top of that page says **Xbox Family Settings sign-in: not signed in** (or when you signed in, or that it expired). Tick **Sign in to Xbox Family Settings (next page)** and submit.
2. **The code sign-in** (the usual way). The next page shows a short code, for example `ABCD-1234`, and an address, `https://www.microsoft.com/link`. On any phone or computer, open that address, enter the code, sign in with the parent account and accept if Microsoft asks for permission. Keep the Home Assistant page open: it goes on by itself once you have signed in (the code is valid for about 15 minutes). Nothing to copy.
3. Home Assistant then makes the device key, checks the whole chain (device token, SISU authorize, XSTS) and saves the sign-in. It checks it with reads only (the Xbox family, if a child's Xbox user id is not known yet, and one read of that child's Xbox overrides, as `probe_xapprove` does first) and shows one of three pages:
   - **Xbox control works**: the box **Use the Xbox Family Settings API for the Xbox lock and extra time** is ticked; submit to turn the xapprove option on.
   - **Xbox refused this sign-in**: the box is unticked; submit. The Xbox lock uses today's Xbox schedule meanwhile. Check that you used the parent account; `probe_xapprove` gives the details.
   - **Signed in, not checked yet** (no child found in the Xbox family, the integration not running, a network error; the reason is shown): set the box as you want it; to be sure, leave it unticked and run `probe_xapprove` first.

If the code expires or the sign-in is declined, a page says so and offers **Get a new code** or **Sign in by pasting the address instead**.

**The pasted-address sign-in** (the fallback). Whether Microsoft offers the code sign-in to the Xbox Family Settings client is not documented. When it refuses (for example `unsupported_response_type` or `invalid_client`), Home Assistant goes straight to this page and says why at the top. This way needs a **computer** with Chrome, Edge or Firefox: the page you end on erases its own address as soon as it has loaded (it becomes `https://login.live.com/oauth20_desktop.srf?removed=true`), so the address cannot be copied from the address bar.

1. Open a new private or incognito window and press **F12**. In the **Network** tab, tick **Preserve log**.
2. In that window, open the page's **sign-in link** (right-click it, copy the link address, and paste it into the window).
3. Sign in with the parent account and accept if Microsoft asks for permission. You end on a blank page.
4. In the Network tab, find the request named **oauth20_desktop.srf** (type it in the filter box), right-click it and choose **Copy > Copy URL**. It starts with `https://login.live.com/oauth20_desktop.srf?code=`.
5. Paste it into the page and submit within a few minutes (the code in it expires soon). Spaces or quotes around it, a missing `https://`, or only the code itself are accepted. Pasting the erased `?removed=true` address is recognised and answered with these steps. Then step 3 above follows.

**The app-style sign-in** (experimental). Tick **Sign in to Xbox Family Settings, app-style (next page)** in Configure when the sign-in above works but Xbox still refuses (`probe_xapprove` answers 401 for every way). It signs in the way the Xbox Family Settings app does: Home Assistant first asks Xbox Live for a sign-in session (`sisu/authenticate`), then you sign in through Microsoft, and Home Assistant authorizes that session (with the consent tokens the app sends), reads your Xbox account the way the app's setup page does (`ReadAccount`, `ReadProfile`; it never creates, reserves, sets or accepts anything) and checks a child's Xbox settings. It keeps the device key and device id of the earlier sign-in and replaces its refresh token.

By default this is **browser-assisted, with no developer tools**: Home Assistant opens the Microsoft sign-in in your browser through its own OAuth proxy (the same one the Family Safety sign-in uses) and reads the result itself.

1. In Configure, tick **Sign in to Xbox Family Settings, app-style (next page)** and submit.
2. On the confirm page, leave **Paste the address myself instead** unticked and submit. Home Assistant opens the Microsoft sign-in; press **Open website** if your browser asks.
3. Sign in with the **parent** account and accept if Microsoft asks. Home Assistant captures the result and returns to the dialog by itself.
4. The same three result pages follow (works / refused / signed in, not checked). When Xbox still refuses and its setup page has a session, the page adds **Finish Xbox setup** with a link to that Xbox page: open it in a browser at once, finish what it asks (Home Assistant never does that itself), then run the app-style sign-in again.

The browser sign-in needs Home Assistant to have an **HTTPS address a browser can reach** (the proxy carries Microsoft credentials). When it has only a local HTTP address, tick the insecure-local-HTTP option in Configure, or use the pasted-address fallback below. If no reachable URL exists, Home Assistant falls back to the fallback automatically and says why.

**Pasted-address fallback.** Tick **Paste the address myself instead** on the confirm page (or Home Assistant sends you here when no browser-reachable HTTPS URL exists). This needs a **computer** with Chrome, Edge or Firefox and its developer tools:

1. Open a new private or incognito window, press **F12**, and in the **Network** tab tick **Preserve log**.
2. In that window, open the page's **sign-in link** (right-click it, copy the link address, paste it into the window).
3. Sign in with the parent account and accept if Microsoft asks. You end on a blank page.
4. In the Network tab, filter on **oauth20_desktop.srf**, right-click the request and choose **Copy > Copy URL**.
5. Paste it into the page and submit right away (the code and the session expire within minutes). The same result pages follow.

The integration then reloads, and from then on xapprove is signed with this sign-in (`working_method` `sisu`), and the Xbox family lookup uses it too.

**When something goes wrong** with the pasted address, the page says what to do: an address that is not the sign-in's last page, the erased `?removed=true` address, the sign-in link itself pasted back, an address from an older link, a declined permission, or a code that expired or was already used (sign in again with the link and paste the new address right away). If Xbox Live itself refuses, nothing is saved. Closed the page by mistake? Open Configure and tick the sign-in again: the code sign-in starts with a new code; on the pasted-address page the link stays the same for 10 minutes, so an address you already copied still works.

**Signing out.** While signed in, Configure also shows **Sign out of Xbox Family Settings**: tick it and submit to forget the sign-in (refresh token and key). To replace a sign-in (another account, or an expired one), just sign in again. Diagnostics show `xbox_xapprove` (option, signed in or not, when, the way of signing in use, the last refusal), nothing secret.

**What is stored**, in Home Assistant's storage (`.storage/microsoft_family_safety.xbox_signin.<entry id>`), never in the options, the logs or the diagnostics: the Microsoft **refresh token** of this sign-in (renewed by Microsoft at each use, and the new one saved), an **EC P-256 private key** made for this entry (its public half, the "ProofKey", is registered with Xbox Live; it signs every Xbox request with the `Signature` header), and a random **device id**. Access, device, user, title and XSTS tokens are kept in memory only. The options only get the time of the sign-in. Anyone with that storage file can act as this Xbox sign-in, as with the integration's other stored sign-in.

**How it keeps working.** The XSTS token lasts about 16 hours; then the refresh token gets a new access token and SISU authorize runs again (the device token, about two weeks, is renewed when it runs out). If Microsoft refuses the refresh token (a password change, a revoked consent, a long pause), a Repairs issue **Sign in to Xbox Family Settings again** appears, the Configure page says the sign-in expired, and the Xbox lock falls back; sign in again as above (the issue goes away once the new sign-in is saved).

**The risk.** This is **unofficial use of a Microsoft first-party client id**. Microsoft does not offer it to third parties, can change the flow or block it at any time, and could object to its use. Nothing breaks when it stops working: the Xbox lock and extra time fall back to the Microsoft web session and, if turned on, today's Xbox schedule, a Repairs issue says so, and you can sign out of Xbox Family Settings in the options.

#### Insecure local HTTP authentication

Native authentication proxies your Microsoft credentials through Home Assistant, so it **requires an HTTPS Home Assistant URL** by default. If your instance is only reachable over plain HTTP on your LAN, you can tick **Allow insecure local HTTP authentication (testing only)**.

This option is deliberately narrow:

- it is accepted **only** for `localhost`, `homeassistant`, `*.local`, or a private / loopback / link-local IP address — a public HTTP URL is rejected with *"Insecure HTTP authentication is allowed only through localhost, .local, or a private/local IP address"*;
- while it is active, your Microsoft password and session data travel **unencrypted across your local network**, and a WARNING is written to the Home Assistant log for every sign-in.

Use it for testing, or on a network you fully trust. Setting up HTTPS is the better answer.

### Re-authenticating

Microsoft sessions expire. The account page session lapses about once a day; since 2.0.6 the integration completes Microsoft's silent sign-in on its own (the same "Continue" step a browser performs), so you should not be asked to sign in again as long as the "Stay signed in?" cookies are valid. When the login itself is gone, Home Assistant raises a **repair / reauthentication** prompt and a persistent notification. That also covers the case where Microsoft has dropped the Family session while the account page still answers: after two consecutive updates in that state the connection sensor reports `degraded` with `reauth_recommended: true` and the reauthentication flow is started for you. You can also start it yourself at any time with the `microsoft_family_safety.request_reauth` service (handy from a dashboard button). Re-authenticating runs the same sign-in and renews the mobile refresh token (and, on an older entry that holds one, the Family web session).

A dead web session does not trigger a reauthentication as long as the schedules answer through the mobile API: the Xbox never uses it. So in practice only an expired mobile token asks you to sign in again. The Xbox Family Settings sign-in is separate: when it needs renewing, a Repairs issue says so (see [Sign in to Xbox Family Settings](#sign-in-to-xbox-family-settings)).

Reauthentication must use the **same Microsoft account** — signing in with a different one aborts with *"A different Microsoft account was used"*.

#### Daily sign-in on some accounts, and a community workaround

Since early October 2026, Microsoft asks some accounts for an interactive Family sign-in every 24 hours, whatever the cookies say (see [#52](https://github.com/noiwid/HAFamilySafety/issues/52)). The connection sensor then reports `signin_required`, and nothing server-side can avoid the sign-in itself.

This no longer affects anything: the Xbox moved to its own [Xbox Family Settings sign-in](#sign-in-to-xbox-family-settings) and never uses the web session, and everything else runs on the mobile token. The integration does not ask for that sign-in on the Xbox's behalf any more.

For older setups that still want the web session renewed unattended, [BartVPL/HAFamilySafety-Auto-Reauth](https://github.com/BartVPL/HAFamilySafety-Auto-Reauth) is a community project that watches the connection sensor, drives the sign-in through Playwright on a separate Linux host, reads the one-time code Microsoft emails through the Gmail API, and completes the flow. It only uses what the integration already exposes (the connection sensor, the `request_reauth` service and the reauth flow). Read its README before installing: it is **not part of this integration, not reviewed here and experimental**. It needs a long-lived Home Assistant token, read-only OAuth access to the mailbox that receives the Microsoft codes, and runs a browser on a machine you must keep secure. Weigh that against signing in by hand once a day.

### Legacy add-on installation

New installs no longer use the add-on (see [Legacy add-on mode](#legacy-add-on-mode)). This section remains for entries that still run on it. Reauthenticating such an entry moves it to the native sign-in, after which the add-on and its standalone container (see [`familysafety-playwright/README.standalone.md`](familysafety-playwright/README.standalone.md)) can be removed.

#### Add-on Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `log_level` | `info` | Logging verbosity (`trace`, `debug`, `info`, `warning`, `error`) |
| `auth_timeout` | `300` | Seconds to wait for user to complete authentication (60-600) |
| `session_duration` | `86400` | Session validity in seconds (1h-7d) |
| `language` | *(auto)* | Browser locale (e.g., `fr-FR`, `en-US`) |
| `timezone` | *(auto)* | Browser timezone (e.g., `Europe/Paris`) |
| `vnc_password` | `familysafety` | Password for the noVNC interface |
| `api_key` | *(auto)* | Only needed when Home Assistant runs on another host |

---

## Devices & Entities

The integration creates two types of HA devices, plus one integration-level diagnostic entity:

| Device Type | Name Example | Manufacturer | Model |
|-------------|-------------|--------------|-------|
| Child account | Firstname Lastname (Family Safety) | Microsoft | Family Safety Account |
| Physical device | DESKTOP-EXAMPLE | From API | From API |

Physical devices are linked to their parent child account via `via_device`.

> **Entity IDs are prefixed with the device name.** A "Sunday Limit" number on the device *Firstname Lastname (Family Safety)* becomes `number.firstname_lastname_family_safety_firstname_sunday_limit`. Switch and button entities use a device named with the first name only, so they are prefixed differently. See [Breaking changes](#breaking-changes). The tables below use `{prefix}` for that device-name prefix.

### Entity count per child

For one child with no per-app switches, an account with Windows monitoring enabled produces roughly:

| Platform | Count | What |
|----------|-------|------|
| `sensor` | 6 – 7 | Screen Time, Account Info, Applications, Pending Requests, Web Filter, Screen Time Policy (+ Balance when the account exposes one) |
| `switch` | 4 + platforms + apps | Account Lock, Screen Time Limits, Xbox Screen Time Limits, App Limits, one Platform Lock per monitored platform (+ the Xbox Lock with the Xbox Family Settings sign-in), one per application |
| `button` | 2 | Approve Request, Deny Request |
| `number` | 7 (+7 Xbox) | one daily limit per day, plus one Xbox daily limit per day once the Xbox schedule is on |
| `time` | 14 | 7 start + 7 end |

Plus **2 sensors per physical device** (screen time, info) and **1 connection sensor per config entry**.

A typical single-child setup with a full app list lands around 85-90 entities.

### Sensors — Per Child Account

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Screen Time | `sensor.{prefix}_screen_time` | Minutes used today | `formatted_time`, `hours`, `minutes`, `average_screentime`, `date`, `raw_microsoft_minutes`, `last_api_poll`, `update_interval_seconds` |
| Account Info | `sensor.{prefix}_account_info` | Full name | `user_id`, `first_name`, `surname`, `profile_picture`, `device_count` |
| Applications | `sensor.{prefix}_applications` | App count | `blocked_count`, `applications` |
| Balance | `sensor.{prefix}_balance` | Account balance | *(monetary sensor, only if available)* |
| Pending Requests | `sensor.{prefix}_pending_requests` | Request count | `requests` |
| Web Filter | `sensor.{prefix}_web_filter` | enabled / disabled / unknown | `blockedSites`, `allowedSites`, `contentRatingAge`, `content_settings`, `max_age_rating`, `acquisition_policy` |
| Screen Time Policy | `sensor.{prefix}_screen_time_policy` | enabled / disabled / unknown | `monday_allowance` … `sunday_allowance`, `*_allowed_intervals`, `daily_restrictions` |
| Xbox Override | `sensor.{prefix}_xbox_override` | block / allow / none (Xbox Family Settings sign-in and xapprove on, child with an Xbox) | `valid_until`, `ours`, `hold`, `lock_path`, `source` (`xapprove`), `read_at`; see [xapprove](#xbox-through-the-xbox-family-settings-api-xapprove) |

### Sensors — Per Physical Device

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Device Screen Time | `sensor.{device}_screen_time` | Minutes used today | — |
| Device Info | `sensor.{device}_info` | Device name | `model`, `OS`, `last_seen` |

### Sensor — Per Integration

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Connection | `sensor.microsoft_family_safety_connection` | connected / degraded / disconnected | `mobile_api`, `web_session`, `web_api`, `family_context`, `screentime_policy_source`, `native_web_auth`, `reauth_required`, `reauth_recommended`, `last_update_success` |

`degraded` means the mobile API works but the Family web session of an older entry does not; the schedules keep working on the mobile API and the Xbox does not use it. An entry without the web session is `connected` whenever the mobile API answers (attribute `web_session_enabled: false`). When that state persists for two consecutive updates, `reauth_recommended` turns `true`, a notification is raised and the reauthentication flow is started automatically. Only a real login redirect, an expired account session or that two-update condition triggers reauthentication; a single rejected request does not, to avoid loops on a merely stale token. This is a **diagnostic** entity; enable it in the entity list if it is hidden.

### Switches — Per Child Account

| Entity | Entity ID | Behavior |
|--------|-----------|----------|
| **Account Lock** | `switch.{prefix}_lock` | **ON = account locked** (all screen time set to 0). Saves quotas before locking, restores on unlock. Persists across restarts. Attribute `has_saved_policy` shows whether a restore point exists. |
| **Screen Time Limits** | `switch.{prefix}_screen_time_limits` | OFF = the Windows schedule is turned off, every day kept as it is. ON = turned back on. Same switch as in the Family Safety app (before 2.0.20 it rewrote every day to 24 h). |
| Xbox Screen Time Limits | `switch.{prefix}_xbox_screen_time_limits` | Turns the child's separate Xbox schedule on or off (the days are kept). Once it is on, seven `number.{prefix}_xbox_{day}_limit` entities set the daily Xbox allowance the console enforces. Mobile API only, so it is created with or without Xbox among the monitored platforms. |
| App Limits | `switch.{prefix}_app_limits` | The "App, feature, and game limits" toggle above the app list. While it is OFF, the per-app time limits are kept but not enforced. |
| App Block | `switch.{prefix}_app_{appname}` | ON = app blocked. One switch per application. |
| Platform Lock | `switch.{prefix}_{platform}_lock` | ON = platform locked. Locks one platform (e.g. Windows) through a Microsoft device override. For Windows the integration tries a web-API time override first, then falls back to the mobile API. Use **Account Lock** to lock every platform at once. |

### Buttons — Per Child Account

| Entity | Entity ID | Action |
|--------|-----------|--------|
| Approve Request | `button.{prefix}_approve_request` | Approves the oldest pending screen time request (+1 hour) |
| Deny Request | `button.{prefix}_deny_request` | Denies the oldest pending request |

### Number Entities — Per Child Account (7 per child)

| Entity | Entity ID | Range | Step |
|--------|-----------|-------|------|
| Daily Limit | `number.{prefix}_{day}_limit` | 0 – 1440 minutes | 15 min |

One entity per day of the week (Sunday through Saturday). Adjustable directly from the UI with **optimistic updates** — values reflect immediately.

### Time Entities — Per Child Account (14 per child)

| Entity | Entity ID | Description |
|--------|-----------|-------------|
| Interval Start | `time.{prefix}_{day}_start` | Start of the allowed screen time window |
| Interval End | `time.{prefix}_{day}_end` | End of the allowed screen time window |

One start/end pair per day of the week, editable from the UI with optimistic updates.

> **The `time.*_end` entities are now populated.** They existed before but stayed `unknown` on many accounts because the end of the allowed window was not parsed from Microsoft's response. Interval parsing now reads the `timeline` array and the *last* allowed interval, so both ends of the window are correct. Microsoft's `24:00:00` end-of-day value is clamped to `23:59`.

---

## Services

The integration exposes **18 services**, split between the pyfamilysafety library and the web API, plus one to start the sign-in flow on demand.

### Account Lock

```yaml
# Lock a child account (sets all screen time to 0, saves current policy)
service: microsoft_family_safety.lock_account
data:
  account_id: "1234567890123456"
```

```yaml
# Unlock a child account (restores saved policy)
service: microsoft_family_safety.unlock_account
data:
  account_id: "1234567890123456"
```

```yaml
# Lock a single platform (use lock_account to lock every platform at once)
service: microsoft_family_safety.lock_platform
data:
  account_id: "1234567890123456"
  platform: "Windows"
  duration_hours: 24
```

Both actions can answer with what they did (`response_variable`): `{prior, applied, lock_path}`. `applied` is `"left_parent_override"` when a parent changed the lock in the Family Safety app since; the answer then has `current: {type, valid_until, override_until, allow_until}`. `lock_path` is `web` (Windows), `xapprove` for an Xbox lock placed through the Xbox Family Settings API (see [Xbox through the Xbox Family Settings API](#xbox-through-the-xbox-family-settings-api-xapprove)), or `mobile_schedule` for an Xbox lock placed through today's schedule when xapprove could not be used (see [The Xbox lock's fallback](#the-xbox-locks-fallback-todays-xbox-schedule)); such a lock ends at midnight at the latest, and its `current` describes the schedule day (`allowance_minutes`, `allotted_intervals`, `schedule_enabled`). Strict mode must stay off when another integration manages these locks (see [Strict mode](#strict-mode)).

#### Extra time

**Xbox:** `add_time_bonus` and `cancel_time_bonus` go through the Xbox Family Settings API (an allow override, captured from the app; see [Xbox through the Xbox Family Settings API](#xbox-through-the-xbox-family-settings-api-xapprove)). They need the Xbox Family Settings sign-in and the xapprove option, and fail with a clear error otherwise.

**Windows** is **experimental: based on an uncaptured request; verified by reading back.** No capture shows how the Family Safety app writes Windows extra time, so `add_time_bonus` sends the captured lock request with `allowUntil` instead of `blockUntil`, then reads the override back and succeeds only when Microsoft reports extra time ending within 2 minutes of what was asked. Otherwise it fails with what was read back (the request and answer are logged at debug level). Extra time replaces a `lock_platform` lock (Microsoft keeps one override per platform), so the lock's hold is released. `cancel_time_bonus` only ever cancels extra time, never a lock. The `hold` attribute reads `bonus_ours` while extra time given here is in force.

```yaml
service: microsoft_family_safety.add_time_bonus
data:
  account_id: "1234567890123456"
  platform: "Xbox"
  minutes: 30
```

```yaml
# Unlock a single platform
service: microsoft_family_safety.unlock_platform
data:
  account_id: "1234567890123456"
  platform: "Windows"
```

### App Management

```yaml
# Block an application
service: microsoft_family_safety.block_app
data:
  account_id: "1234567890123456"
  app_id: "app-uuid"
```

```yaml
# Unblock an application
service: microsoft_family_safety.unblock_app
data:
  account_id: "1234567890123456"
  app_id: "app-uuid"
```

```yaml
# Set a per-app daily time limit with allowed window
service: microsoft_family_safety.set_app_time_limit
data:
  account_id: "1234567890123456"
  app_id: "app-uuid"
  app_name: "Minecraft"
  platform: "windows"
  hours: 1
  minutes: 30
  start_time: "08:00:00"
  end_time: "20:00:00"
```

```yaml
# Remove a per-app time limit
service: microsoft_family_safety.remove_app_time_limit
data:
  account_id: "1234567890123456"
  app_id: "app-uuid"
  app_name: "Minecraft"
  platform: "windows"
```

### Screen Time

`set_screentime_limit` and `set_screentime_intervals` accept an optional
`platform` field, either `Windows` or `Xbox`. Omitting it keeps the existing
Windows behavior. Select `Xbox` explicitly to update the separate Xbox schedule.
Xbox writes require an unambiguous `PerDeviceType` schedule and never fall back
to a Windows write if the Xbox request fails. The same checks apply to existing
Xbox daily-limit entities because they use the shared Xbox writer.

```yaml
action: microsoft_family_safety.set_screentime_intervals
data:
  account_id: "1234567890123456"
  platform: Xbox
  day_of_week: 1
  start_hour: 18
  end_hour: 20
  end_minute: 30
```

```yaml
# Set daily screen time allowance
service: microsoft_family_safety.set_screentime_limit
data:
  account_id: "1234567890123456"
  day_of_week: 1  # 0=Sunday, 6=Saturday
  hours: 2
  minutes: 0
```

```yaml
# Set allowed time window (30-min precision)
service: microsoft_family_safety.set_screentime_intervals
data:
  account_id: "1234567890123456"
  day_of_week: 1
  start_hour: 8
  start_minute: 0
  end_hour: 20
  end_minute: 30
```

### Request Handling

```yaml
# Approve a pending screen time request (+N minutes)
service: microsoft_family_safety.approve_request
data:
  request_id: "request-uuid"
  extension_minutes: 60
```

```yaml
# Deny a pending request
service: microsoft_family_safety.deny_request
data:
  request_id: "request-uuid"
```

### Web Filtering

```yaml
# Block a website
service: microsoft_family_safety.block_website
data:
  account_id: "1234567890123456"
  website: "example.com"
```

```yaml
# Allow a website (always-allowed list)
service: microsoft_family_safety.allow_website
data:
  account_id: "1234567890123456"
  website: "example.com"
```

```yaml
# Remove a website from the blocked or allowed list
service: microsoft_family_safety.remove_website
data:
  account_id: "1234567890123456"
  website: "example.com"
```

```yaml
# Toggle web content filtering
service: microsoft_family_safety.toggle_web_filter
data:
  account_id: "1234567890123456"
  enabled: true
```

### Content & Purchase Controls

```yaml
# Set age rating (PEGI 3-20, or 21 for unrestricted)
service: microsoft_family_safety.set_age_rating
data:
  account_id: "1234567890123456"
  age: 12
```

```yaml
# Enable or disable ask-to-buy
service: microsoft_family_safety.set_acquisition_policy
data:
  account_id: "1234567890123456"
  require_approval: true
```

### Xbox token probe (diagnostic)

`probe_xbox_token` checks, inside Home Assistant, whether the Family Safety sign-in the integration holds can also get an Xbox Live token. (Live, xapprove refused every such token, so the Xbox uses the separate Xbox Family Settings sign-in; this probe stays as a diagnostic and is not used by anything else.) It uses the stored mobile refresh token to ask `login.live.com` for scope `service::user.auth.xboxlive.com::MBI_SSL`, then tries Xbox user authentication (RPS ticket `t=`, then `d=`) and XSTS (`http://xboxlive.com`, `RETAIL`). It answers per step with HTTP status, error codes, the granted scope, expiry in seconds and the **names** of the XSTS `xui` claims. No token, gamertag or Xbox user id is returned or logged, and a refresh token in Microsoft's answer is thrown away: the integration keeps its own.

**The risk, and why `confirm_risk: true` is required.** Microsoft documents for its identity platform that a refresh token is not tied to a scope and that using it does not revoke it ([Refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens)), and the integration already rotates this token through the same endpoint without trouble. It does not document this for the older `login.live.com/oauth20_token.srf` endpoint used here. If that endpoint ever revoked the old token on use, the integration would need a new sign-in. The probe holds off the integration's own token refresh while it runs, then runs one normal refresh and reports it as `family_refresh_after` (`ok: false` means: sign in again). Administrators only; `entry_id` is needed only with several entries.

```yaml
service: microsoft_family_safety.probe_xbox_token
data:
  confirm_risk: true
response_variable: probe
```

### Xbox Family Settings API probe (diagnostic)

`probe_xapprove` checks, inside Home Assistant and without writing anything, whether the Xbox through xapprove works with the [Xbox Family Settings sign-in](#sign-in-to-xbox-family-settings) (without that sign-in it answers `xbox_signed_in: false` and an `error`, and calls nothing). It runs that sign-in's token steps, looks up the children's Xbox user ids, and reads the first resolved child's override (`GET /api/v1/user/{xuid}/settings/screentime/overrides`), trying each way of signing that read in turn until one answers 200, all with the app's own request headers, each signed with the device key (`_signed`) and unsigned (`_unsigned`): an XSTS token from the app's explicit XSTS step for `http://xboxlive.com` (`xsts_*`); the same after replaying the sign-in's web view reads (`ReadAccount`, `ReadProfile` on `sisu.xboxlive.com/proxy`, as the app's web page does; read-only), with the sign-in's tokens (`webview_xsts_signed`) or with a new authorization after it (`webview_reauth_xsts_signed`); the `no_title` and `sisu_device` XSTS tokens as two more ways (`xsts_no_title_signed`, `xsts_sisu_device_signed`); the same with a session from `sisu/authenticate` passed to the authorization, without and with the consent tokens (`session_xsts_signed`, `session_ucs_xsts_signed`); the same after a sign-in authorization that also carries the consent service tokens the app sends, in eight forms (`ucs_<raw|t>_<prompts|noprompts>_<noc|c1>_xsts_signed`, and two with the sign-in's own XSTS, `ucs_raw_prompts_noc_sisu_signed`, `ucs_t_prompts_noc_sisu_signed`); the sign-in's own XSTS (`sisu_*`), also with contract version 1, 2 or 3 (`sisu_c1_*`, `sisu_c2_*`, `sisu_c3_*`); and an MSA token of that client for the xapprove scope as `WLID1.0 t=` (`msa_wlid_*`) or `XBL3.0` (`msa_xbl_*`). Some ways also read the screen-time settings (`.../settings/screentime`, the app's first call); a read there alone is reported (`screentime_read_ok`), not saved. The XSTS step itself is tried in variants until one answers 200 (`no_claims`, the default; `no_title`, `sisu_device`, `unsigned`, `plain_json`, `app`), each its own entry in `steps`; the variant that worked is saved with the way. It also reports the title id (`title_id`) and what one diagnostic `sisu/authenticate` answers (`sisu_authenticate`: status, error code, field and header names, and whether a session id and a sign-in redirect came back, never their values), the Xbox title endpoints (`endpoints`: which host, relying party and signature policy xapprove and the related services ask for), the sign-in's web page (`web_page` in `steps.sisu_authorize`: host, path, page name, query and fragment key names, the shape of its signature; `web_page_fetch`: what the page and its scripts would call), each web view read tried (`sisu_proxy[...]`: status, error code, field names) and the way that answered (`proxy_way`). The first way that answers 200 is **saved and used by the integration from then on**, also after a restart (`working_method`, `method_saved`); until one has, the integration uses `xsts_signed`, the way the app does it. The Family Safety sign-in is never used.

It answers with non-secret facts only: `identity` (`xbox_family_settings`), `xbox_signed_in`, `xbox_signin_error` (the failing step, status and XErr when the sign-in itself failed), `steps` (`sisu_msa_refresh`, `device_authenticate`, `sisu_authorize`, `xsts_authorize[<relying party>]`: HTTP status, XErr, which tokens came back and the XSTS `xui` claim **names**), `title_token_received`, `sisu_xui_keys` and `xsts_xui_keys` (per relying party tried; claim names only), `parent_xuid_claim_present`, the family lookup (`members`, `members_with_xuid`, `adults`, `signed_in_parent_listed`, the distinct `roles` and the member field **names**), per child `resolved` and `via` (`option`, `family_id`, `family_name`, `elimination`), `overrides_attempts` (per attempt: `name`, `path`, `stage` `token` or `xapprove`, `status`, `xerr`, `step`/`error` for a token failure, `www_authenticate` `{name, value}` when xapprove sends that header, and for a refused read every response header name, the values of the error, authentication and correlation headers and the first 300 characters of the body, with anything token-like or id-like replaced), `xsts` (per relying party: the variant that worked and the claim names), `working_method`, and `overrides_read` (`ok`, `method`, `override_active`, `type`, `raw_type`, `valid_until`, `actions`; or `status`/`xerr` of the first way when none worked). It needs `confirm_risk: true` (the unofficial client id, see the sign-in's risk note) and is for administrators. No token, gamertag or Xbox user id is returned or logged.

```yaml
service: microsoft_family_safety.probe_xapprove
data:
  confirm_risk: true
response_variable: probe
```

### Reauthentication

Starts the Microsoft sign-in flow immediately, without waiting for the integration to detect an expired session. Useful behind a dashboard button (the example card's connection pill opens the integration page for the same purpose). No fields.

```yaml
service: microsoft_family_safety.request_reauth
```

---

## Automation Examples

> The examples below use short placeholder entity IDs (`switch.firstname_lock`). **Your actual IDs include the device-name prefix** — e.g. `switch.firstname_family_safety_firstname_lock`. Copy the real IDs from **Developer Tools > States**, or rename the entities to the short form. See [Breaking changes](#breaking-changes).

### Lock account on school nights

```yaml
automation:
  - alias: "Lock account at 21:00"
    trigger:
      - platform: time
        at: "21:00:00"
    condition:
      - condition: time
        weekday: [sun, mon, tue, wed, thu]
    action:
      - action: switch.turn_on
        target:
          entity_id: switch.firstname_lock

  - alias: "Unlock account at 07:00"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - action: switch.turn_off
        target:
          entity_id: switch.firstname_lock
```

### Screen time alert

```yaml
automation:
  - alias: "Screen time limit alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.firstname_screen_time
        above: 120
    action:
      - action: notify.mobile_app_your_phone
        data:
          title: "Screen Time Alert"
          message: >
            {{ state_attr('sensor.firstname_screen_time', 'formatted_time') }}
            of screen time used today.
```

### Set weekday limits automatically

```yaml
automation:
  - alias: "Set weekday screen time limits"
    trigger:
      - platform: time
        at: "00:05:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    action:
      - action: microsoft_family_safety.set_screentime_limit
        data:
          account_id: "1234567890123456"
          day_of_week: "{{ now().weekday() }}"
          hours: 1
          minutes: 30
```

### Watchdog — prevent manual unlock

```yaml
automation:
  - alias: "Anti-bypass watchdog"
    trigger:
      - trigger: state
        entity_id: switch.firstname_lock
        to: "off"
    condition:
      - condition: time
        after: "21:00:00"
        before: "07:00:00"
    action:
      - action: switch.turn_on
        target:
          entity_id: switch.firstname_lock
```

### Dashboard card

A clean per-child panel is available as a [decluttering-card](https://github.com/custom-cards/decluttering-card) template, so you drop one card per child and only change a few variables. It includes:

- A header with the day's progress bar and allowed window, plus a clickable connection-health pill (opens the integration to re-authenticate)
- Screen time used, blocked-app count, pending-request count
- When a request is pending: what is being asked (platform and time of the request, tooltip with Microsoft's message), with Approve (grants 1 hour) / Deny buttons
- Lock, Windows-lock and screen-time-limits switches
- Today's top apps, and the weekly limits grid (tap to edit the limit, hold to edit the window)

Two files:

- [`examples/family-safety-card.yaml`](examples/family-safety-card.yaml) — the `fs_child` template only.
- [`examples/dashboard.yaml`](examples/dashboard.yaml) — a full example view instantiating it with placeholder entities.

**Required Marketplace dashboard cards:** `decluttering-card`, `button-card`, `vertical-stack-in-card`, `card-mod`, `mushroom`

Paste the `decluttering_templates:` block into your dashboard (a dashboard has exactly one such key — merge, do not duplicate), then add one `custom:decluttering-card` per child, filling `child`, `lock` and `title`. Entity IDs carry the device-name prefix; look them up under **Settings → Devices & Services → Entities**. See the comments at the top of each file.

To add more children, add one `custom:decluttering-card` block per child in the same view; the shared template updates every card at once.

---

## Troubleshooting

### The sign-in window shows "400 Bad Request"

Home Assistant's built-in `security_filter` middleware rejects URLs whose query string looks like a file-injection attempt, using the pattern `[a-zA-Z0-9_]=/([a-z0-9_.]//?)+`. Some of Microsoft's silent-SSO redirects carry parameters such as `epctrc=/w/...`, which match that pattern, and the request is refused before the integration ever sees it.

The integration carries such a query in an encoded form on its way through Home Assistant and restores it before it reaches Microsoft. The filter itself is not changed.

If you still hit a 400 during authentication, retry the flow; check the Home Assistant log for `security_filter` entries, and open an issue with the (redacted) URL.

### Microsoft asks "Work or school account" or "Personal account"

This page appears when the same address exists as both kinds of Microsoft account, sometimes again after you have signed in. Pick **Personal account**: Family Safety only works with it. The setup then continues on its own.

### "Microsoft authentication host not allowed"

Microsoft's sign-in page links some of its own pages with an explicit port (`login.microsoftonline.com:443`), and versions before 2.0.4 rejected that host. Update to 2.0.4 or later; the port is ignored now. If you still see it, the host in the address bar after `__ms_host__/` is not one of the sign-in hosts (`account.microsoft.com`, `account.live.com`, `login.live.com`, `login.microsoft.com`, `login.microsoftonline.com`), which the proxy deliberately refuses. Please open an issue with that host name.

### Sign-in never finishes / stuck on the waiting screen

- The waiting screen after the visible sign-in can legitimately sit for up to about **a minute** without updating. Keep the Microsoft window open, do not click **Open website** again and do not close the dialog while it is running.
- The authentication proxy expires **10 minutes** after it is created. If you took longer, start the flow again.
- **On a phone** (Home Assistant app or mobile browser) the Microsoft page opens in a separate browser. Once it shows *Authentication completed*, close it and switch back to Home Assistant: since 2.0.9 the dialog picks up the result within a few seconds of coming back. Before 2.0.9 it could stay stuck because Android pauses the app while the sign-in runs; signing in from a desktop browser avoids that on older versions.
- If Home Assistant aborts with *"Native web authentication could not be loaded"* or *"The browser authentication flow expired"*, simply restart the flow.


### "502 Bad Gateway" from nginx during sign-in

nginx logs `upstream sent too big header while reading response header from upstream`. Microsoft's sign-in pages answer with large cookie headers, and the integration's sign-in proxy passes them on, which overflows nginx's default response-header buffer (4 or 8 KB). Raise it in the `location` that proxies Home Assistant:

```nginx
proxy_buffer_size       128k;
proxy_buffers           4 256k;
proxy_busy_buffers_size 256k;
```

Nginx Proxy Manager: add the same three lines under the host's **Advanced** tab. Then restart the sign-in.

Official "NGINX Home Assistant SSL proxy" add-on: in the add-on configuration, enable **Customize** > `active`, keep the default file names (`nginx_proxy_default*.conf`), put the same three lines in `/share/nginx_proxy_default_proxy_buffer.conf` and restart the add-on.

### "Automatic browser authentication normally requires HTTPS"

Your Home Assistant URL is not HTTPS. Either configure HTTPS (recommended), or enable **Allow insecure local HTTP authentication (testing only)** — which only works for local hostnames and private IP addresses. See [Insecure local HTTP authentication](#insecure-local-http-authentication).

### Screen time reads return `unknown`

- Check the **Connection** sensor: `screentime_policy_source` should be `mobile_v4`. If it is not, the mobile schedule read failed and the integration fell back to the web session.
- If both fail, the most common cause is an expired Microsoft session. Home Assistant raises a reauthentication prompt and a persistent notification — complete it.
- After a network or timeout error on the schedule endpoint, the integration backs off for **30 minutes** before retrying. If a fix seems to have no effect, you may be inside that window; reload the integration to clear it.
- Versions before 2.0.3 could stay in this state indefinitely without ever asking you to re-authenticate: a rejected Family request left the captured session marked valid, so the check that detects an expired login never ran again. If you are on an older version and the schedule has been `unknown` for days while the mobile sensors keep updating, update and reload; the reauthentication prompt will appear.

### Screen time writes fail

- Writes go to the mobile API first and to the web API only as a fallback. If reads are also `unknown`, fix authentication first.
- Microsoft answers the schedule writes slowly: a lock or unlock (7 days, allowance and time window each) can take about a minute.
- A partial failure raises an error naming how many of the 7 weekdays were updated, and **keeps the saved restore point** so you can safely retry.

### Account Lock issues

- **Lock refuses to run** with *"current schedule unreadable and no saved policy exists"* — this is the safety guard from issue #23. It prevents wiping a child's real schedule when the current one cannot be read and there is nothing to restore from. The message tells you which case you are in: an expired session (re-authenticate), or a timeout / network error / active backoff (retry in a few minutes).
- **Unlock refuses to run** with *"no saved schedule to restore"* — the restore point is gone, typically because `.storage/microsoft_family_safety.saved_screentime.<entry_id>` was deleted. The integration will not invent a schedule; set the daily limits again from Home Assistant or at [account.microsoft.com/family](https://account.microsoft.com/family). Before 2.0.3 it wrote a 2 h/day, 07:00-22:00 default here, which silently replaced the real schedule.
- **Lock is account-wide** — it affects all platforms simultaneously.
- Lock and unlock **stop at the first network failure** rather than waiting out a timeout for each of the seven days, and keep the saved schedule so the operation can be retried.

### "Microsoft cannot resolve part of the family roster"

Microsoft's mobile API sometimes answers `Family.UnableToFindTargetResource` / `RosterError` (HTTP 404) for one child, typically when a reset or decommissioned device is still listed in the family, or when a school/work (Entra ID) account is enrolled on the child's device. Since 2.0.7 the integration keeps running: the affected child keeps empty values for the data Microsoft refuses (devices, screen-time usage, apps, lockable platforms or spending), everything else loads normally, and a persistent notification plus the `roster_errors` attribute of the Connection sensor tell you which member and which data are affected. Removing the stale device at https://account.microsoft.com/family clears it when that is possible; a school-managed device usually cannot be removed, in which case the degraded data for that child is expected.

### Legacy add-on mode

- Make sure the add-on is **started** (green icon in the Add-ons page).
- The integration resolves the add-on hostname dynamically via the Supervisor API, or uses the add-on URL stored in the entry when it was created.
- If the session is dead (redirect to a marketing page), the simplest fix since 2.0.8 is to reauthenticate the entry from Home Assistant: it switches the entry to the native sign-in (answer Yes to "Stay signed in?"). Re-authenticating via the noVNC interface still works if you want to stay on the add-on.
- Add-on writes take ~20-30 s each because the browser must reach the family dashboard first.

### Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Useful markers: `family_token_present`, `exported_cookies`, `bootstrap_attempts` during sign-in; `Microsoft Family context requires browser authentication` when the Family session needs re-establishing.

---

## Security & limitations

### Security

Native authentication trades some of the add-on's isolation for simplicity. Be aware of the following before enabling it:

- **Credentials are stored in clear text.** Microsoft session cookies, the Family antiforgery token and the OAuth refresh token are persisted in the config entry and in `.storage/` **unencrypted**. The legacy add-on encrypted its cookie file with Fernet; the native path does not. Anyone with read access to your Home Assistant configuration directory (including backups) can extract a usable Microsoft session. Protect and encrypt your backups accordingly.
- **The authentication proxy endpoint is unauthenticated.** `/auth/microsoft_family_safety/proxy/{token}` and its callback are registered without Home Assistant authentication — the only protection is a 24-byte random token in the URL. The flow is scoped: the proxy only serves the Microsoft sign-in hosts (`account.microsoft.com`, `account.live.com`, `login.live.com`, `login.microsoft.com`, `login.microsoftonline.com`), the callback only accepts that token for its own flow, it exists for at most **10 minutes**, and it is destroyed when the flow finishes. Still, while a flow is live, anyone who can reach your Home Assistant HTTP interface **and** guess the token would be proxied to Microsoft.
- **The Microsoft login page is served from your Home Assistant origin.** Because the sign-in is proxied, the address bar shows your Home Assistant URL rather than `login.live.com`, so you cannot verify the Microsoft certificate visually. Only start the flow from Home Assistant itself.
- **HTTP mode is genuinely insecure.** With `allow_insecure_http_auth` enabled, your Microsoft password crosses the local network unencrypted. It is restricted to local/private addresses and logs a warning, but it remains a testing option, not a deployment mode.

### Limitations

- **Unofficial API** — Microsoft provides no public API for Family Safety. This integration relies on reverse-engineered endpoints that may change or break at any time.
- **Renewal depends on the Microsoft-account cookies.** The integration captures Microsoft's cookie rotations, re-fetches the antiforgery token when it goes stale and, since 2.0.6, completes the daily silent sign-in of the account page by itself. It cannot recover from a revoked or expired Microsoft login (password change, security review, cookies past their date); Home Assistant will prompt you then. With "Stay signed in?" answered Yes, the captured cookies are valid for about a year.
- **Akamai bot-manager cookies are deliberately discarded.** Microsoft fronts `account.microsoft.com` with Akamai, whose `bm_sv` / `ak_bmsc` cookies are tied to the browser that obtained them. Replaying them from Home Assistant made Microsoft stop answering entirely (the request hung until the timeout, then the connection was reset), which hid an expired session behind timeouts and backoffs. They are dropped wherever cookies are captured, loaded or saved.
- **A browser is still required to sign in.** The Family Safety sign-in and the Xbox Family Settings sign-in are both interactive. There is no fully headless sign-in.
- **No Xbox play time.** It came from the Family web session's activity report, which the integration no longer uses for the Xbox. The Xbox Allowed Now sensor knows the schedule and the override in force, not the minutes played.
- **Entity IDs changed** — see [Breaking changes](#breaking-changes).
- **Per-platform lock is unreliable** — Microsoft removed the `override_device` endpoint. Account Lock is the recommended replacement, but it locks all platforms at once.
- **Legacy add-on mode is serialized** — the add-on uses a single browser instance with a lock, so concurrent requests are queued.

---

## API Reference

Microsoft Family Safety exposes **two distinct APIs**. This integration uses both, each for specific capabilities.

### Mobile API — `mobileaggregator.family.microsoft.com`

**Authentication:** OAuth Bearer token (acquired via `pyfamilysafety`)

Used for read operations and app management. Token-based, no browser session required.

| Method | Endpoint | Description | Used by |
|--------|----------|-------------|---------|
| GET | `/v1/WebRestrictions/{childId}` | Web filter settings & blocked sites | `sensor.web_filter` |
| GET | `/v1/ContentRestrictions/{childId}` | Age rating & ask-to-buy state | `sensor.web_filter` attributes |
| GET | `/v1/DeviceLimits/{childId}/overrides` | Active device overrides | `switch.lock` |
| PATCH | `/v4/devicelimits/schedules/{childId}` | ~~Set screen time schedule~~ | **Broken** (400 error) |
| POST | `/v1/devicelimits/{childId}/overrides` | ~~Lock device~~ | **Broken** (Microsoft removed) |

> The mobile API's schedule and device override endpoints no longer work reliably. All screen time writes now go through the web API.

### Web API — `account.microsoft.com/family/api/`

**Authentication:** Microsoft session cookies + the `__RequestVerificationToken` antiforgery token, read from the authenticated Family dashboard and sent as a header of the same name. All requests require the headers `X-AMC-JsonMode: CamelCase`, `X-Requested-With: XMLHttpRequest`, a plausible `Referer`, and — for writes — `Content-Type: application/json` and `Origin: https://account.microsoft.com`. Some endpoints additionally require a per-child `X-JwtFamilyRelationshipToken`, harvested from the roster or landing-page feed.

> Only the token named `__RequestVerificationToken` works. The generic `canary` / `apiCanary` values look similar but belong to a different antiforgery context and produce HTTP 401.

**In native mode these calls are issued directly by Home Assistant over HTTP** (httpx), with no browser and no add-on, once the Family session has been established. In [legacy add-on mode](#legacy-add-on-mode) they are executed from inside the add-on's authenticated Chromium session instead.

#### Read Endpoints (GET)

| Endpoint | Query Params | Description | Used by |
|----------|-------------|-------------|---------|
| `/family/api/roster` | — | Family members list | Coordinator, relationship tokens |
| `/family/api/st` | `childId` | Screen time policy (per-device, Windows) — **tried first** | `sensor.screen_time_policy`, `number.*_limit`, `time.*_start/end` |
| `/family/api/landing-page-feeds` | `memberIdList` | Dashboard feed; fallback source for the weekday schedule, and source of relationship tokens | `sensor.screen_time_policy`, `number.*_limit`, `time.*_start/end` |
| `/family/windows/home/direct`, `/family/home` | — | Family dashboard pages, scraped for `__RequestVerificationToken` | Session bootstrap |
| `/account` | — | Session health probe | `sensor.*_connection` |
| `/family/api/screen-time-global` | `childId` | Global screen time toggle | — |
| `/family/api/device-limits/get-devices` | `childId` | Connected devices list | — |
| `/family/api/app-limits/get-all-app-policies-v3` | `childId` | All app policies | `switch.app_*` |
| `/family/api/app-limits/get-app-time-extension-requests` | `memberIdList` | Pending extension requests | `sensor.pending_requests`, `button.approve/deny` |
| `/family/api/recent-activity/report-v3` | `childId`, `isPreviousWeek`, `timeZone` | Activity report | `sensor.screen_time` |
| `/family/api/settings/web-browsing` | `childId` | Web filter settings | `sensor.web_filter` |

#### Write Endpoints (POST/PUT/DELETE)

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/family/api//st/day-allow` | `{childId, dayOfWeek, timeSpanDays, timeSpanHours, timeSpanMinutes}` | Set daily screen time allowance |
| POST | `/family/api//st/day-allow-int` | `{childId, dayOfWeek, allowedIntervals: [48 booleans]}` | Set allowed time intervals (30-min slots) |
| POST | `/family/api/app-limits/set-custom-app-policy-v3` | `{childId, appPolicy: {...}, platform}` | Block/unblock/limit an app |
| POST | `/family/api/settings/block-website` | `{childId, website}` | Block a website |
| DELETE | `/family/api/settings/remove-website` | `?childId=&website=` | Remove a blocked/allowed website |
| POST | `/family/api/settings/web-browsing-toggle` | `{childId, isEnabled}` | Toggle web content filtering |
| PUT | `/family/api/settings/update-content-settings` | `{childId, contentRatingAge}` | Set age rating (3-20, 21=unrestricted) |
| POST | `/family/api/ps/set-acquisition-policy` | `{childId, policy}` | Set ask-to-buy (`freeOnly` / `unrestricted`) |

#### Screen Time Response Structure

`GET /family/api/st?childId={childId}` returns:

```json
{
  "userId": "1234567890123456",
  "isEnabled": true,
  "dailyRestrictions": {
    "monday": {
      "dayOfWeek": "monday",
      "allowance": "01:00:00",
      "allowedIntervals": [
        {
          "begin": "PT7H",
          "beginTimeSpan": "07:00:00",
          "end": "PT23H",
          "endTimeSpan": "23:00:00"
        }
      ],
      "timeline": [false, false, ..., true, true, ..., false, false]
    }
  }
}
```

- `allowance`: daily limit as `HH:MM:SS`
- `allowedIntervals[].beginTimeSpan/endTimeSpan`: window boundaries as `HH:MM:SS`
- `timeline`: 48 booleans representing 30-min slots (index 0 = 00:00, index 14 = 07:00)

### Home Assistant authentication proxy (native mode)

Registered by the integration only while a sign-in flow is running, and destroyed when it finishes or after 10 minutes. Both routes are served **without Home Assistant authentication**; the random per-flow token in the URL is the only guard. See [Security & limitations](#security--limitations).

| Method | Endpoint | Description |
|--------|----------|-------------|
| any | `/auth/microsoft_family_safety/proxy/{token}[/{path}]` | Reverse proxy to Microsoft sign-in (restricted to `account.microsoft.com`, `account.live.com`, `login.live.com`, `login.microsoft.com`, `login.microsoftonline.com`) |
| GET | `/auth/microsoft_family_safety/callback?flow_id=` | Hands the captured result back to the config flow |

### Addon API — `http://{addon-hostname}:8098` *(legacy mode only)*

The addon exposes a local HTTP API that proxies requests through the authenticated browser session. It is not used in native mode.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check |
| POST | `/api/auth/start` | Start authentication session |
| GET | `/api/auth/status/{session_id}` | Check auth session status |
| GET | `/api/cookies/check` | Check cookie freshness |
| GET | `/api/cookies` | Get stored cookies |
| DELETE | `/api/cookies` | Delete stored cookies |
| GET | `/api/screentime?childId=` | Fetch screen time via browser |
| POST | `/api/screentime/set-allowance` | Set daily allowance via browser |
| POST | `/api/screentime/set-intervals` | Set time intervals via browser |

### Capabilities Matrix

| Action | Web API | Mobile API | Status |
|--------|:---:|:---:|--------|
| View family roster | GET | — | Working |
| View screen time usage | — | GET | Working |
| Read screen time schedule | GET | — | Working (direct from HA) |
| Set daily screen time allowance | POST | ~~PATCH~~ | Working (web only) |
| Set allowed time intervals | POST | ~~PATCH~~ | Working (web only) |
| Block/unblock an app | POST | POST | Working |
| Set per-app time limits | POST | — | Working |
| Block/allow a website | POST/DELETE | — | Working |
| Toggle web filtering | POST | — | Working |
| Set content age rating | PUT | — | Working |
| Set ask-to-buy policy | POST | — | Working |
| **Lock device (instant)** | **N/A** | ~~POST~~ | **Broken** (Microsoft removed) |
| Lock account (workaround) | POST x14 | — | Working (sets all quotas to 0) |
| Lock the Xbox, Xbox extra time | — | — | Working through the Xbox Family Settings API (`xapprove.xboxlive.com`, its own sign-in); the lock falls back to today's Xbox schedule (mobile API) |

---

## Contributing

Contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes and open a pull request

Areas where help is especially appreciated:
- Microsoft API endpoint documentation and analysis
- Encrypting the stored Microsoft cookies and tokens at rest
- Additional language translations
- Testing native authentication across different Family Safety account configurations and Home Assistant setups

---

## Acknowledgments

- **[pantherale0](https://github.com/pantherale0)** — original [ha-familysafety](https://github.com/pantherale0/ha-familysafety) integration and [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety) library
- The **Home Assistant** community for feedback and testing

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Support

- [GitHub Issues](https://github.com/noiwid/HAFamilySafety/issues)
- [GitHub Discussions](https://github.com/noiwid/HAFamilySafety/discussions)

When reporting an issue, please include: HA version, integration version, whether you use native or legacy add-on authentication (the **Connection** sensor's `native_web_auth` attribute tells you), the add-on version if applicable, debug logs, and steps to reproduce.

[releases-shield]: https://img.shields.io/github/release/noiwid/HAFamilySafety.svg?style=for-the-badge
[releases]: https://github.com/noiwid/HAFamilySafety/releases
[license-shield]: https://img.shields.io/github/license/noiwid/HAFamilySafety.svg?style=for-the-badge
[license]: LICENSE
[marketplace-shield]: https://img.shields.io/badge/Marketplace-Default-41BDF5.svg?style=for-the-badge
[marketplace]: https://my.home-assistant.io/redirect/hacs_repository/?owner=noiwid&repository=HAFamilySafety&category=integration
