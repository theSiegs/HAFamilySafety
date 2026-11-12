# <img src="https://raw.githubusercontent.com/noiwid/HAFamilySafety/main/custom_components/microsoft_family_safety/brand/icon.png" alt="Microsoft Family Safety" width="30" > Microsoft Family Safety for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![Marketplace][marketplace-shield]][marketplace]
[![License][license-shield]][license]
[![Buy Me A Beer](https://img.shields.io/badge/Buy%20me%20a%20beer-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/noiwid)

A full-featured Home Assistant custom integration for **Microsoft Family Safety**. Monitor screen time, manage app restrictions, lock accounts, control web filtering, and adjust daily limits — all from your Home Assistant dashboard.

Authentication is **native**: you sign in to Microsoft once, from Home Assistant itself. After that the integration runs on the long-lived token of the Family Safety app (and, for the Xbox, on the Xbox Family Settings sign-in) and never visits Microsoft's web pages on its own.

**Supported platforms:** Windows, Xbox, Mobile

> **Domain:** `microsoft_family_safety` | **IoT Class:** Cloud Polling | **Languages:** English, French, German

![Dashboard Example](https://raw.githubusercontent.com/noiwid/HAFamilySafety/main/examples/dashboard.png)

⭐ **Enjoying this integration?** [Star it on GitHub](https://github.com/noiwid/HAFamilySafety): it gives the project visibility and helps other parents discover it.

## Also supervising Android devices?

[Google Family Link for Home Assistant](https://github.com/noiwid/HAFamilyLink) is the companion integration for Android phones and tablets. With both installed, all of your child's screen time can be followed in one place, in Home Assistant. This also makes a time budget shared across devices possible, something Google and Microsoft cannot do across each other's devices. It is not built in: an automation adds up `sensor.{prefix}_screen_time` and `sensor.<child>_daily_screen_time`, then locks the devices once the budget is spent.

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

### The Family web session is no longer used

The integration no longer keeps a Microsoft Family **web session** (the `account.microsoft.com` cookies captured at sign-in). Microsoft makes some accounts renew that session with an interactive sign-in every 24 hours ([#52](https://github.com/noiwid/HAFamilySafety/issues/52)), and the integration's own attempts to keep it alive (the `/account` probe, the Family dashboard bootstrap, the silent "Continue" renewal) are what landed on Microsoft's sign-in pages and sent security codes to the parent's phone.

Existing entries keep working on their mobile token, without a new sign-in: the stored cookies are deleted from the entry at the first start. What changes:

- **Xbox lock and Xbox play time** come only from the [Xbox Family Settings sign-in](#xbox). It is linked during the Microsoft sign-in when Microsoft allows it, otherwise from the integration options. Without it, the Xbox lock switch reports an error instead of locking, the Xbox play time is not added to the screen time, and a repair issue asks you to link it.
- **Windows platform lock** always uses the mobile API's device override (the web time override is gone).
- **Reauthentication** is only asked when Microsoft refuses the integration's refresh token. A lapsed web session, the Family dashboard asking for a sign-in, or missing cookies no longer start one. A refused Xbox Family Settings sign-in raises a repair issue asking for a new Xbox link instead.
- **Connection sensor**: the web-session attributes are gone (see [Sensor — Per Integration](#sensor--per-integration)); `degraded` now means the Xbox Family Settings link is missing or refused while Xbox is ticked.
- **Sign-in**: every entry signs in through the Family Safety app's own page. "Stay signed in?" no longer matters, and an HTTP-only instance can always use the manual redirect step, for a reauthentication too.

### The add-on is no longer used

The Playwright auth add-on is not contacted any more. Entries created with it keep working on their mobile token, without a new sign-in; the add-on (and its standalone container) can be uninstalled.

---

## Architecture

The integration authenticates natively inside Home Assistant and talks to Microsoft's backends itself. **No add-on, no browser automation, and no separate container are needed.**

### The Microsoft APIs

| API | Base URL | Auth Method | Capabilities |
|-----|----------|-------------|-------------|
| **Mobile API** | `mobileaggregator.family.microsoft.com` | OAuth Bearer token (from the long-lived refresh token of the Family Safety app, client `000000000004893A`) | Family roster, devices, usage, screen time schedules (Windows and Xbox), account lock, Windows/Mobile platform lock, app blocking and app time limits, websites, age rating, ask-to-buy, requests. |
| **Xbox Family Settings** | `xapprove.xboxlive.com` | Refresh token of the Xbox Family Settings app (client `00000000482c8f49`), exchanged for Xbox Live tokens | The Xbox schedule the console enforces, the Xbox lock and Xbox play time. |

Both run on refresh tokens, the same way Microsoft's own apps work; neither needs a browser session. Microsoft's private web API (`account.microsoft.com/family/api`) is not used.

### How native authentication works

You sign in **once**, in your normal browser, through a short-lived reverse proxy that Home Assistant mounts inside its own HTTP server. The proxy serves Microsoft's mobile OAuth page, the one the Family Safety app uses, and captures the redirect carrying the authorization code; that code becomes the refresh token for the mobile API.

With **Xbox** among the monitored platforms, Home Assistant then asks Microsoft, with the cookies of that same sign-in and only during the sign-in, for an authorization code of the Xbox Family Settings app. When Microsoft answers without another sign-in page, the Xbox link is made with no extra step; otherwise link it from the integration options (see [Xbox](#xbox)). The sign-in cookies are discarded at the end of the flow: only the two refresh tokens are kept.

### After sign-in: no browser at all

After the sign-in, Home Assistant talks to both APIs **directly over HTTPS** from the integration, on the refresh tokens only. It never loads `account.microsoft.com` or a Microsoft sign-in page on its own.

### Data flow

```
Poll loop (default every 5 min)
  ├─ Mobile API            → roster, devices, apps, usage, schedules (Windows + Xbox),
  │                          websites, content restrictions, app limits switch
  └─ Xbox Family Settings  → Xbox schedule, Xbox lock state, Xbox play time (linked only)

Service call / entity write
  ├─ Mobile API            → schedules, account lock, Windows/Mobile platform lock,
  │                          app blocking and app limits, websites, age rating,
  │                          ask-to-buy, requests, Xbox limits (when not linked)
  └─ Xbox Family Settings  → Xbox lock, Xbox limits (linked only)
```

---

## Features

| Category | What you can do |
|----------|----------------|
| **Native authentication** | Sign in to Microsoft directly from Home Assistant — no add-on, no container, no copy-pasted redirect URL |
| **Account Lock** | Lock/unlock a child's entire account with a single switch |
| **Screen time monitoring** | Track daily usage per child and per device |
| **Allowed now / time left** | See per child and platform (Windows, Xbox) whether it may be used right now, the minutes left today, and when downtime starts and ends |
| **Screen time policies** | Adjust daily allowances and allowed time intervals per day — directly from the UI |
| **Today-only changes** | Give more (or less) time or other allowed hours for today only; the weekly values come back at midnight |
| **App management** | Block/unblock apps, set per-app time limits and windows, pause apps and resume them with their limits intact |
| **Web filtering** | Block/unblock domains (one or several per call), read the blocked and allowed lists, keep a published blocklist in sync, toggle content filtering (action or switch), set PEGI age ratings |
| **Purchase controls** | Enable/disable ask-to-buy via service call |
| **Request handling** | Approve or deny pending screen time requests from HA |
| **Connection diagnostics** | A dedicated sensor reports whether the mobile API and the Xbox Family Settings sign-in are healthy |
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
3. Set the **update interval** and the **monitored platforms** (Windows, Xbox, Mobile). Tick **Xbox** only if your children use one. Click **Submit**
4. Home Assistant shows an **Open website** button. Click it: a browser window opens on the Microsoft sign-in page, served through Home Assistant's temporary authentication proxy
5. Sign in with your **Microsoft parent account** (the family organizer, not a child account) and complete MFA if prompted
6. As soon as Microsoft hands back the token, the window closes itself; Home Assistant shows a short progress screen while it fetches the tokens, then creates the integration. With Xbox ticked, Home Assistant also tries to link the Xbox Family Settings sign-in in the same step (see [Xbox](#xbox))
7. The integration discovers all child accounts and devices automatically. Screen-time entities can read `unknown` for up to one update interval (5 minutes by default) while the first data pull completes; that is expected

> The old flow — copy an auth URL, sign in, paste the redirect URL back — is gone in the normal case. It survives as a fallback on HTTP-only instances.

### Integration Options

**Settings > Devices & Services > Microsoft Family Safety > Configure**:

| Option | Range / Format | Default | Notes |
|--------|----------------|---------|-------|
| Update interval (seconds) | 30 – 3600 | 300 (5 min) | |
| Monitored platforms | Windows / Xbox / Mobile | Windows | One lock switch per platform. Xbox also adds the Xbox entities, see [below](#xbox) |
| Users allowed to control Microsoft Family Safety | list of Home Assistant users | *(empty: everybody)* | See [Who can control the integration](#who-can-control-the-integration) |
| App usage sensors per child | 0 – 30 | 0 (off) | How many of each child's most used apps today get an [App Usage](#sensors--per-child-account) sensor; also turns on the [`microsoft_family_safety_app_usage`](#events) event |
| Strict mode | on / off | off | Places a `lock_platform` lock again when it is cancelled elsewhere; see [Strict mode](#strict-mode) |
| Allow insecure local HTTP authentication (testing only) | on / off | off | See below |
| Link the Xbox Family Settings sign-in | on / off | off | Only with Xbox ticked; for when the Connection sensor shows `xbox_family: not_linked`. Opens a Microsoft sign-in through the same temporary proxy as the setup, which picks up the result itself: sign in, close the window, done. A passkey does not work through the proxy (it is tied to Microsoft's address), here or in the main sign-in: choose *Sign in another way*. Without an HTTPS URL (or the local HTTP option below), or if the sign-in does not finish within 10 minutes, you paste the address of the blank page Microsoft ends on instead |

#### Who can control the integration

Home Assistant lets every user press any button, flip any switch or change any number, unless an entity permission policy says otherwise, and Home Assistant has no screen to write such policies. If your child has a Home Assistant account, they could otherwise lift their own limits from the integration's entities.

The option **Users allowed to control Microsoft Family Safety** closes that gap:

- **Empty** (default): nothing changes, every user can use the buttons and settings.
- **Some users ticked**: only they and the administrators can. Any other account is refused, with a warning in the log, on every button, switch, daily limit and time window of the integration, and on every `microsoft_family_safety.*` action.
- Automations and scripts started by Home Assistant itself are never affected. A script started by a person from the dashboard runs with that person's rights.

Give the child a **non-administrator** account and tick the parents: an administrator can change this option, so a child with an administrator account bypasses it. The child still sees the entities (Home Assistant cannot hide them per user) but can no longer use them. Changing the option takes effect immediately, without reloading the integration.

#### Strict mode

When on, a lock placed by `microsoft_family_safety.lock_platform` that Microsoft reports as cancelled before its end (released in the Family Safety app, for example) is placed again on the next update, at most every 10 minutes, and the event `microsoft_family_safety_lock_reasserted` is fired (`entry_id`, `account_id`, `platform`, `until`). Only an explicit "nothing in force" answer counts: a failed read never triggers a write. `unlock_platform` ends it. Windows and Mobile locks are read back from the mobile API; an Xbox lock only with the Xbox Family Settings sign-in linked.

**Keep strict mode off when another integration or automation manages the locks**: it would lock again right after a parent's own unlock in the Family Safety app and fight the parent's decision. Such an integration should read the `hold` attribute of the platform lock switches instead (see [Platform locks and a parent's own changes](#platform-locks-and-a-parents-own-changes)).

#### Xbox

The Xbox lock, the Xbox limits and Xbox play time go through the **Xbox Family Settings** app's service, on its own refresh token. Microsoft moved the Xbox settings there: its per-day limits are the ones the console enforces, and an Xbox override stored by the Family Safety mobile API is accepted but ignored by the console.

- **Xbox ticked** among the monitored platforms: the Xbox entities are created (Xbox lock, Xbox schedule switch, Xbox daily limits). The Xbox Family Settings sign-in is linked during the Microsoft sign-in when Microsoft allows it. Otherwise a repair issue appears (and the Connection sensor shows `xbox_family: not_linked`): open the options, tick **Link the Xbox Family Settings sign-in**, open the link shown, sign in with the parent account and paste the address of the blank page you end up on. If Microsoft later refuses that sign-in, it is retried every 6 hours and a repair issue asks you to link it again the same way.
- **Xbox unticked** (the default for new installs): no Xbox entities are created and no Xbox Family Settings link is made.

Without the link, the Xbox daily limits and the Xbox schedule switch still work through the mobile API, but the Xbox lock fails with an error (the mobile API's Xbox override does nothing on the console) and the Xbox play time is not read. Children are matched to their Xbox account by first name and surname; a child whose names differ between Microsoft and Xbox is reported in `xbox_family_unmatched_children` and has no Xbox lock.

Unticking Xbox later takes effect at once. Xbox entities created earlier stay in the entity list as unavailable; remove them there if you like.

#### Insecure local HTTP authentication

Native authentication proxies your Microsoft credentials through Home Assistant, so it **requires an HTTPS Home Assistant URL** by default. If your instance is only reachable over plain HTTP on your LAN, you can tick **Allow insecure local HTTP authentication (testing only)**.

This option is deliberately narrow:

- it is accepted **only** for `localhost`, `homeassistant`, `*.local`, or a private / loopback / link-local IP address — a public HTTP URL is rejected with *"Insecure HTTP authentication is allowed only through localhost, .local, or a private/local IP address"*;
- while it is active, your Microsoft password and session data travel **unencrypted across your local network**, and a WARNING is written to the Home Assistant log for every sign-in.

Use it for testing, or on a network you fully trust. Setting up HTTPS is the better answer.

### Re-authenticating

The mobile refresh token is long-lived and renewed by the integration on its own; you are not asked to sign in again as long as Microsoft accepts it. When Microsoft refuses it (password change, security review, revoked sign-in), Home Assistant raises a **repair / reauthentication** prompt. A refused Xbox Family Settings sign-in does not: it only affects the Xbox features, and a repair issue asks you to link it again from the options (see [Xbox](#xbox)). You can also start a reauthentication yourself at any time with the `microsoft_family_safety.request_reauth` service (handy from a dashboard button). Re-authenticating runs the same sign-in, renews the refresh token and, with Xbox ticked and the Xbox link missing or refused, links the Xbox Family Settings sign-in again.

Reauthentication must use the **same Microsoft account** — signing in with a different one aborts with *"A different Microsoft account was used"*.

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
| `sensor` | 8 – 11 | Screen Time, Account Info, Applications, Pending Requests, Web Filter, Websites Visited, Screen Time Policy, Windows Time Remaining (+ Balance when the account exposes one, + Xbox Screen Time and Xbox Time Remaining with Xbox data, + App Usage sensors when turned on) |
| `binary_sensor` | 2 (+2 Xbox) | Windows Allowed Now, Windows Downtime Active, plus the same two for the Xbox once its schedule is read |
| `switch` | 5 + platforms + apps | Account Lock, Screen Time Limits, Xbox Screen Time Limits, App Limits, Web Filtering, one Platform Lock per monitored platform, one per application |
| `button` | 2 | Approve Request, Deny Request |
| `number` | 7 (+7 Xbox) | one daily limit per day, plus one Xbox daily limit per day once the Xbox schedule is on |
| `time` | 14 | 7 start + 7 end |

Plus **2 sensors per physical device** (screen time, info) and **1 connection sensor per config entry**.

A typical single-child setup with a full app list lands around 85-90 entities.

### Sensors — Per Child Account

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Screen Time | `sensor.{prefix}_screen_time` | Minutes used today | `formatted_time`, `hours`, `minutes`, `average_screentime`, `date`, `raw_microsoft_minutes`, `last_api_poll`, `update_interval_seconds`, `apps` (the apps used today, see below) |
| Account Info | `sensor.{prefix}_account_info` | Full name | `user_id`, `first_name`, `surname`, `profile_picture`, `device_count` |
| Applications | `sensor.{prefix}_applications` | App count | `blocked_count`, `blocked_apps` (up to 20 names), `top_apps` (the 5 most used today), `applications` (every app as Microsoft lists it, with icons), `apps` (every app, see below), `paused_apps` (apps paused by `pause_app`: `app_id`, `app_name`, `paused`: `ours` while still blocked, `parent` once someone unblocked them). `applications` and `apps` are not stored in the history database: with icons the list can exceed its size limit |
| App Usage | `sensor.{prefix}_{app}_usage` | Minutes the app was used today | `app_id`, `app_name`, `platform`, `blocked`. Opt-in: only with **App usage sensors per child** above 0, for each child's most used apps today, up to that number. An app keeps its sensor once it has one |
| Balance | `sensor.{prefix}_balance` | Account balance | *(monetary sensor, only if available)* |
| Pending Requests | `sensor.{prefix}_pending_requests` | Request count | `requests` |
| Web Filter | `sensor.{prefix}_web_filter` | enabled / disabled / unknown | `blockedSites`, `allowedSites`, `blocked_site_count`, `allowed_site_count`, `contentRatingAge`, `content_settings`, `max_age_rating`, `acquisition_policy` |
| Websites Visited | `sensor.{prefix}_websites_visited` | Visits in Edge over the last 7 days | `report_available`, `site_count`, `blocked_count`, `sites` (50 most visited: `domain`, `visits`, `last_visit`, `blocked`), `searches` (50 latest search terms, one row per term: `search`, `visits`, `date`, `site`; times in UTC with an offset). Read every 30 min; `report_available: false` when Microsoft has no Edge data for the child. `sites` and `searches` are not stored in the history database |
| Screen Time Policy | `sensor.{prefix}_screen_time_policy` | enabled / disabled / unknown | `monday_allowance` … `sunday_allowance`, `*_allowed_intervals`, `daily_restrictions`; with the Xbox Family Settings sign-in linked, also `xbox_monday_allowed_intervals` … `xbox_sunday_allowed_intervals` (the Xbox allowed hours, e.g. `[{begin: "07:00", end: "22:00"}]`), `today_only` (pending today-only changes, see [Today only](#today-only)) |
| Windows Time Remaining | `sensor.{prefix}_windows_time_remaining` | Minutes the child may still use Windows today | `allowed_now`, `reason`, `allowance_minutes`, `used_minutes`, `limits_enabled`, `allowed_ranges`. Same rules as Allowed Now below |
| Xbox Screen Time | `sensor.{prefix}_xbox_screen_time` | Minutes of Xbox play today | Created with the Xbox features on, once Xbox data was read |
| Xbox Time Remaining | `sensor.{prefix}_xbox_time_remaining` | Minutes the child may still play on the Xbox today | As Windows Time Remaining, on the Xbox schedule, Xbox play time and Xbox lock or extra time |

### Binary Sensors — Per Child Account and Platform

One of each for Windows, and for the Xbox once its schedule has been read. They only use what the integration already polls (schedule, today's usage, locks and extra time) and are re-evaluated every minute, since allowed hours move with the clock between updates.

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Allowed Now | `binary_sensor.{prefix}_{platform}_allowed_now` | ON while the platform may be used now | `reason`, `allowance_minutes`, `used_minutes`, `in_allowed_hours`, and the same `hold`, `override`, `override_until`, `allow_until`, `platform`, `user_id` as the Platform Lock switch (see [below](#platform-locks-and-a-parents-own-changes)). The minutes left are on Time Remaining only, so this entity writes a state when something other than the clock changes |
| Downtime Active | `binary_sensor.{prefix}_{platform}_downtime_active` | ON outside today's allowed hours | `downtime_start`, `downtime_end` (the current downtime, or the next one, local ISO times) |

**How Allowed Now and Time Remaining decide**, in this order:

1. A lock in force (`reason: locked`) → off, 0 minutes left. Extra time granted (`extra_time`) → on until it ends.
2. Limits turned off for the platform (`no_limits`) → on, counting down to midnight.
3. Outside today's allowed hours (`outside_allowed_hours`) → off, 0 minutes.
4. Today's allowance used up (`allowance_used`) → off, 0 minutes. Otherwise on (`within_limits`), with the smaller of the allowance left and the time to the end of the current allowed window.

Windows uses the Windows schedule and the minutes of the child's Windows devices; the Xbox uses the Xbox schedule, Xbox play time and the lock or extra time in the Xbox policy. The state is `unknown` (`reason: unknown`) while the schedule or today's usage has not been read, for example for a child without a Windows device. `used_minutes` (and `allowed_ranges` on Time Remaining) are not stored in the history database.

- All the time logic (allowed hours, today's allowance, midnight) uses Home Assistant's time zone, not the child's device's.
- Windows extra time is not taken into account: Microsoft gives no Windows "allowed until" time. After an approved request for more time, Windows Allowed Now can stay off with `reason: allowance_used` (and Time Remaining at 0) while the child is in fact allowed. Xbox extra time is read from the Xbox policy and counts.

**The `apps` attribute** is a compact list of the child's apps, most used today first, without icons:

```yaml
apps:
  - package: "x:1234567890"   # Microsoft's app id; the prefix gives the platform
    name: "Racing Game"
    minutes: 42.0             # minutes used today
    platform: Xbox            # Windows (appx:, win32:), Xbox (x:), Mobile (a:) or null
```

An app used on two platforms has a different id on each, so it is listed once per platform. The Screen Time sensor lists only the apps used today; the Applications sensor lists every app.

### Sensors — Per Physical Device

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Device Screen Time | `sensor.{device}_screen_time` | Minutes used today | — |
| Device Info | `sensor.{device}_info` | Device name | `model`, `OS`, `last_seen` |

### Sensor — Per Integration

| Entity | Entity ID | State | Key Attributes |
|--------|-----------|-------|----------------|
| Connection | `sensor.microsoft_family_safety_connection` | connected / degraded / disconnected | `mobile_api`, `roster_errors`, `screentime_policy_status`, `xbox_features`, `xbox_family`, `xbox_family_linked_via`, `xbox_family_last_error`, `xbox_family_matched_children`, `xbox_family_unmatched_children`, `reauth_required`, `reauth_reason`, `last_update_success` |

`connected` means the mobile API answers. `degraded` means it does, but with Xbox ticked the Xbox Family Settings link is missing (`xbox_family: not_linked`) or refused (`xbox_family: auth_failed`): only the Xbox lock and Xbox play time are affected, and a repair issue says how to link it. `disconnected` means the mobile API does not answer. `screentime_policy_status` is `ok`, `no_policy_data`, `network_error` or `error` for the last Windows schedule read. This is a **diagnostic** entity; enable it in the entity list if it is hidden.

### Switches — Per Child Account

| Entity | Entity ID | Behavior |
|--------|-----------|----------|
| **Account Lock** | `switch.{prefix}_lock` | **ON = account locked** (all screen time set to 0). Saves quotas before locking, restores on unlock. Persists across restarts. Attribute `has_saved_policy` shows whether a restore point exists. |
| **Screen Time Limits** | `switch.{prefix}_screen_time_limits` | OFF = the Windows schedule is turned off, every day kept as it is. ON = turned back on. Same switch as in the Family Safety app (before 2.0.20 it rewrote every day to 24 h). |
| Xbox Screen Time Limits | `switch.{prefix}_xbox_screen_time_limits` | Turns the child's separate Xbox schedule on or off (the days are kept). Once it is on, seven `number.{prefix}_xbox_{day}_limit` entities set the daily Xbox allowance the console enforces; with the Xbox Family Settings sign-in linked, each also shows that day's Xbox allowed hours in its `allowed_intervals` attribute. Not created without Xbox among the monitored platforms. |
| App Limits | `switch.{prefix}_app_limits` | The "App, feature, and game limits" toggle above the app list. While it is OFF, the per-app time limits are kept but not enforced. |
| Web Filtering | `switch.{prefix}_web_filtering` | ON = Microsoft filters inappropriate websites and searches in Microsoft Edge, and the blocked and allowed lists apply. Same write as the `toggle_web_filter` action. |
| App Block | `switch.{prefix}_app_{appname}` | ON = app blocked. One switch per application. |
| Platform Lock | `switch.{prefix}_{platform}_lock` | ON = platform locked. Windows and Mobile are locked through the mobile API's device override, the Xbox through the [Xbox Family Settings](#xbox) sign-in (an error without it). Use **Account Lock** to lock every platform at once. Turning it off cancels whatever lock is in force (it does not put back an earlier one, unlike `unlock_platform`). Attributes `user_id`, `platform`, `blocked`, and who decides the lock: `hold` (`ours`, `bonus_ours`, `parent` or `none`), `override` (`blockUntil`, `allowUntil`, `none`, or null when unreadable), `override_until` and `allow_until` (local ISO time or null), see [below](#platform-locks-and-a-parents-own-changes). |

### Buttons — Per Child Account

| Entity | Entity ID | Action |
|--------|-----------|--------|
| Approve Request | `button.{prefix}_approve_request` | Approves the oldest pending screen time request (+1 hour) |
| Deny Request | `button.{prefix}_deny_request` | Denies the oldest pending request |

### Number Entities — Per Child Account (7 per child)

| Entity | Entity ID | Range | Step |
|--------|-----------|-------|------|
| Daily Limit | `number.{prefix}_{day}_limit` | 0 – 1440 minutes | 15 min |

One entity per day of the week (Sunday through Saturday). Adjustable directly from the UI with **optimistic updates** — values reflect immediately. A whole-day allowance, which Microsoft sends as `1.00:00:00`, reads as 1440 (earlier versions read it as 0).

### Time Entities — Per Child Account (14 per child)

| Entity | Entity ID | Description |
|--------|-----------|-------------|
| Interval Start | `time.{prefix}_{day}_start` | Start of the allowed screen time window |
| Interval End | `time.{prefix}_{day}_end` | End of the allowed screen time window |

One start/end pair per day of the week, editable from the UI with optimistic updates.

> **The `time.*_end` entities are now populated.** They existed before but stayed `unknown` on many accounts because the end of the allowed window was not parsed from Microsoft's response. Interval parsing now reads the `timeline` array and the *last* allowed interval, so both ends of the window are correct. Microsoft's `24:00:00` end-of-day value is clamped to `23:59`.

### Events

| Event | When | Data |
|-------|------|------|
| `microsoft_family_safety_request` | A new pending request (more time, an app) appears. Checked on every update; the first update after a start only sets the baseline, and a request must be missing for three updates in a row before it counts as new again | `entry_id`, `account_id`, `first_name`, `request_id`, `type`, `platform`, `app_id`, `app_name`, `requested_time` (as Microsoft gives it) |
| `microsoft_family_safety_app_usage` | An app's minutes today grew between two updates (at least 0.5 min). Only with **App usage sensors per child** above 0. The first update of a day, or after a restart, only sets the baseline | `entry_id`, `account_id`, `first_name`, `app_id`, `app_name`, `platform`, `grew_minutes`, `today_minutes` |
| `microsoft_family_safety_request_resolved` | A pending request left the list. Answered with `approve_request` / `deny_request`: on the next update, with `via: service`, `outcome` (`approved` / `denied`) and `minutes`. Answered elsewhere (the Family Safety app): once it has been gone for three updates in a row, with `via: app` and `outcome: unknown` (the mobile API does not say how) | `entry_id`, `account_id`, `request_id`, `outcome`, `minutes`, `via` |

```yaml
automation:
  - alias: "Tell me about new requests"
    trigger:
      - platform: event
        event_type: microsoft_family_safety_request
    action:
      - action: notify.mobile_app_your_phone
        data:
          message: >
            {{ trigger.event.data.first_name }} asks for
            {{ trigger.event.data.app_name or 'more screen time' }}
            ({{ trigger.event.data.platform }}).
```

---

## Services

The integration exposes **25 services**, split between the pyfamilysafety library, the integration's own mobile API calls and the Xbox Family Settings service, plus one to start the sign-in flow on demand.

Every service that acts on one child takes the child either as `account_id` (the Microsoft user ID, shown in the `user_id` attribute of the child's entities) or as `entity_id`: any Microsoft Family Safety entity of that child or of one of its devices. `device_id` works the same way: the child's Microsoft Family Safety device or one of its physical devices. The account is looked up from the entity or device, so automations need no hard-coded ID. Lists are accepted as long as everything in them belongs to the same child. When `account_id` is given, it wins.

These can go under `data:` or under `target:`. A `target:` may name entities and devices only: `area_id`, `floor_id` and `label_id` are refused, since an area can hold several children's devices.

```yaml
# Same as account_id: "1234567890123456", read from the child's lock switch
service: microsoft_family_safety.lock_account
target:
  entity_id: switch.firstname_lock
```

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
  duration_hours: 24        # or until: "2026-01-31 20:30:00" (local time), not both
```

```yaml
# Unlock a single platform
service: microsoft_family_safety.unlock_platform
data:
  account_id: "1234567890123456"
  platform: "Windows"
```

#### Extra time (Xbox and Windows)

`add_time_bonus` gives a child extra time, for some `minutes` (1 – 1440) or `until` a time (at most 24 hours ahead); `cancel_time_bonus` takes it back. `platform` is `"Xbox"` (the default) or `"Windows"`. Neither uses the web session.

- **Xbox** goes through the Xbox Family Settings service (the same "add time" as the Xbox Family Settings app), so it needs **Xbox** among the monitored platforms and the Xbox Family Settings sign-in linked; otherwise it fails with an error saying so.
- **Windows** goes through the device-limits overrides of the Family Safety mobile API (the same endpoint as the Windows lock), with an `AllowUntil` override. No published client sends that request, so Windows extra time is believed only when Microsoft lists it on a read right after the write, ending within 2 minutes of the time asked. If it is not listed, whatever the write left is cancelled (never a lock) and the action fails with "Microsoft did not confirm extra time on Windows" and what was done.
- Microsoft keeps **one** override per platform, so extra time replaces a lock. Before writing, the overrides are read: while the platform is locked (by `lock_platform` or by a parent in the app), `add_time_bonus` is refused unless you pass `replace_lock: true`.
- The extra time is believed only when Microsoft lists it ending when asked; otherwise the action fails. If a lock is still listed afterwards, the action fails and the lock stays.
- While extra time runs, the lock switch of that platform reads off (Microsoft reports `allowUntil` / `AllowUntil`).
- Extra time given here is remembered as the integration's own: the `hold` attribute reads `bonus_ours` (not `parent`) while it is in force. For Windows, whose extra time is not polled, the integration's own record of it stands in until it ends.
- Given with `replace_lock: true` over a lock placed by `lock_platform`, the integration keeps that lock's hold: strict mode never locks over the extra time, and once the extra time ends (or `cancel_time_bonus` ends it) the lock goes back on until its own end. `unlock_platform` during that extra time forgets the lock and leaves the extra time running (`applied: "bonus_kept"`).
- A lock that extra time replaced and that the integration did not place (a parent's lock from the app, or one already lifted by `unlock_platform`) does not come back: after `cancel_time_bonus`, or when the extra time ends, the platform follows its schedule again. Lock it again with `lock_platform` if needed.
- `cancel_time_bonus` only ever cancels extra time. While the platform is locked it fails without sending anything (the cancel would lift the lock too); with no extra time in force it does nothing.
- Both can answer with what they did (`response_variable`): `add_time_bonus` with `{added, until, applied_until, replaced_lock, hold_kept}`, `cancel_time_bonus` with `{cancelled: true, lock_in_force}` (plus `lock_restored: true` and `lock_until` when a lock of the integration's was put back) or `{cancelled: false, reason: "no_extra_time"}`.

```yaml
# 30 minutes of extra time on the Xbox
service: microsoft_family_safety.add_time_bonus
data:
  account_id: "1234567890123456"
  minutes: 30
```

```yaml
# One more hour even though the Xbox is locked: the lock is replaced
service: microsoft_family_safety.add_time_bonus
data:
  account_id: "1234567890123456"
  minutes: 60
  replace_lock: true
response_variable: extra_time
```

```yaml
# End the Xbox extra time early (the child as one of its entities)
service: microsoft_family_safety.cancel_time_bonus
data:
  entity_id: switch.firstname_xbox_lock
```

```yaml
# 45 minutes of extra time on Windows
service: microsoft_family_safety.add_time_bonus
data:
  account_id: "1234567890123456"
  platform: "Windows"
  minutes: 45
response_variable: extra_time
```


#### Platform locks and a parent's own changes

`lock_platform` and `unlock_platform` are safe to call from automations while a parent also uses the Family Safety app:

- The **first** `lock_platform` of a child's platform reads the override Microsoft has in force (a parent's own lock, extra time, or nothing) and saves it, with the fact that the integration now holds a lock there. Repeated locks keep that first saved state, also after a restart or a failed call. A lock already in force that ends later is kept, never shortened. If a parent extended the lock in the app since, a repeat lock leaves the parent's lock in force and does not take it over.
- `unlock_platform` puts the saved lock back if it has not run out yet, otherwise cancels. Saved extra time is not granted again. It only undoes a lock that is still the integration's own: if a parent put another lock or extra time in force since, it is left alone. When Microsoft shows nothing in force although the integration holds the lock, or the state cannot be read, the cancel is still sent (harmless if nothing is there), so a child is never left locked by a read that missed the lock.
- The override in force is read from the Xbox Family Settings service for Xbox and from the mobile API for Windows and Mobile, and written the same way; the web session is not used. A failed read is tried once more; if it still fails the lock goes ahead with the prior state "unknown", and `unlock_platform` then only cancels a lock it can identify as its own.

Both actions can answer with what they did (`response_variable`), `{prior, applied}`:

| `applied` | Meaning |
|-----------|---------|
| `{type: blockUntil, valid_until}` | A lock was sent (by `lock_platform`, or a saved lock put back by `unlock_platform`) |
| `{type: none}` | The lock was cancelled (`not_restored` lists saved extra time that was not granted again) |
| `"left_parent_override"` | Someone changed the lock since; nothing was sent. `current: {type, valid_until, override_until, allow_until}` tells what is in force |
| `"parent_lock_kept"` | (`lock_platform`) A longer lock that is not the integration's is in force; nothing was sent, see `current` |
| `"hold_expired"` | The integration's lock had already run out; nothing was sent |
| `"not_locked"` | The integration's lock never went through; nothing was sent |
| `"bonus_kept"` | (`unlock_platform`) Extra time the integration gave is in force (it replaced the lock); nothing was sent and the extra time keeps running, see `current` |

`unlock_platform` adds `prior_unknown: true` when the state before the lock could not be read, and `state_unknown: true` when the state at unlock could not be read. `until` may be at most 168 hours away, like `duration_hours`.

```yaml
- action: microsoft_family_safety.unlock_platform
  data:
    account_id: "1234567890123456"
    platform: "Xbox"
  response_variable: result
- if: "{{ result.applied == 'left_parent_override' }}"
  then:
    - action: notify.notify
      data:
        message: "A parent changed the Xbox lock in the Family Safety app; left as it is."
```

The platform lock switches show the same as attributes, for automations that must not fight a parent:

| Attribute | Values |
|-----------|--------|
| `hold` | `ours` (the integration's lock, still as it was placed), `bonus_ours` (extra time given by `add_time_bonus` is in force), `parent` (a lock or extra time the integration did not place, or its own lock changed elsewhere), `none` (nothing in force, nothing held) |
| `override` | `blockUntil`, `allowUntil`, `none`, or null when it could not be read |
| `override_until` | When the lock or extra time in force ends (local ISO time), or null |
| `allow_until` | When extra time in force ends (local ISO time), or null |
| `platform` / `user_id` | The platform (`Windows`, `Xbox`, `Mobile`) and the child's account id (a string) |

For Windows and Mobile only the "locked" flag is polled, without an end, so `override_until` stays null there and extra time is not seen. The Xbox values come from the Xbox Family Settings service and are null without its sign-in linked.

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

```yaml
# Pause Windows or Xbox apps for now (blocks them, keeping their time limit)
service: microsoft_family_safety.pause_app
data:
  account_id: "1234567890123456"
  apps:
    - "x:app-id"
    - "appx:app-id"
```

```yaml
# Resume paused apps: puts back the time limit (or none) each had before.
# Leave out apps to resume every paused app of the child.
service: microsoft_family_safety.resume_app
data:
  account_id: "1234567890123456"
  apps:
    - "x:app-id"
```

`pause_app` first reads each app's policy (from Microsoft's app activity report) and saves it in Home Assistant's storage, so it survives a restart. An app with a time limit is blocked with its limit kept stored (a plain `block_app` would wipe the limit); an app without one is blocked like `block_app` does. A second pause of the same app keeps what the first one saved. Apps that are already blocked are skipped and never touched, and an app whose policy cannot be read is not paused, since there would be nothing safe to put back. `resume_app` reads the policy again and writes the saved one back unblocked. An app that someone unblocked in the meantime is reported as `released`, and one whose limit or block a parent changed as `changed_by_parent`; both are left as the parent set them. The app's block switch and `unblock_app` resume a paused app the same way (its limit comes back), and `block_app` on a paused app keeps it paused with its limit stored. A block or unblock never wipes a Windows or Xbox app's limit either: an app already in the asked state is left as it is, an app with a limit is blocked or unblocked with its limit kept (so unblocking it later puts the limit back), and an app that cannot be read is not blocked or unblocked. Mobile apps still get Microsoft's plain block and unblock. Saved states stay until resumed or until the integration entry is removed. Both services answer with what they did, per app, when called with `response_variable`, and fail when every app failed:

```yaml
# pause_app
{"account_id": "1234567890123456", "paused": ["x:app-id"], "changed_by_parent": [], "skipped": {"appx:other": "already blocked"}, "failed": {}}
# resume_app
{"account_id": "1234567890123456", "resumed": ["x:app-id"], "released": [], "changed_by_parent": [], "skipped": {}, "failed": {}}
```

### Screen Time

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

#### Xbox daily limit and allowed hours

Both services take an optional `platform`: `Windows` (the default, unchanged behaviour) or `Xbox`. With `Xbox` they write the child's Xbox schedule through the Xbox Family Settings service, the one the console enforces:

- `set_screentime_limit` sets the day's Xbox limit and keeps its allowed hours;
- `set_screentime_intervals` sets the day's Xbox **allowed hours** (the time window the child may play in, like a bedtime) and keeps its limit. The window lies within one day, on the hour or half hour; `end_hour: 24` means midnight.

Xbox must be among the monitored platforms, the Xbox Family Settings sign-in must be linked and the child must match one child of the Xbox family by first and last name. Otherwise the action fails with a message saying which of these is missing, and nothing is written: the Xbox schedule of the Family Safety mobile API is not used as a fallback, because the console no longer enforces it ([#70](https://github.com/noiwid/HAFamilySafety/issues/70)). The allowed hours only apply while the child's Xbox schedule is on (**Xbox Screen Time Limits** switch).

```yaml
# Xbox allowed hours on Fridays: 16:00 to 21:30
action: microsoft_family_safety.set_screentime_intervals
data:
  account_id: "1234567890123456"
  platform: Xbox
  day_of_week: 5
  start_hour: 16
  end_hour: 21
  end_minute: 30
```

```yaml
# Xbox limit on Saturdays: 3 hours
action: microsoft_family_safety.set_screentime_limit
data:
  account_id: "1234567890123456"
  platform: Xbox
  day_of_week: 6
  hours: 3
```

#### Today only

Microsoft's limits are weekly: a change to Monday's limit applies to every
Monday. Add `today_only: true` to change today only, for a day off school, a
half day or a bonus day. `day_of_week` can then be left out (if given, it must
be today's).

```yaml
# 3 h on the Xbox today, back to the usual limit at midnight
action: microsoft_family_safety.set_screentime_limit
data:
  account_id: "1234567890123456"
  platform: Xbox
  hours: 3
  today_only: true
```

```yaml
# Windows allowed until 23:00 today only
action: microsoft_family_safety.set_screentime_intervals
data:
  account_id: "1234567890123456"
  start_hour: 7
  end_hour: 23
  today_only: true
```

How it behaves:

- Today's current value is read from Microsoft first and kept; only the fields
  you change are kept (limit and allowed hours separately). The call is refused,
  before anything is written, when today's value cannot be read.
- At the next local midnight (Home Assistant's time zone) the kept value is
  written back. If Home Assistant was not running at midnight, it is written at
  the first poll after the restart.
- It is written back only while the day still holds the value set here. If a
  parent changed it in the meantime (in the Family Safety app, or with a normal
  call or entity for that day), the parent's value stays.
- A second today-only call on the same day keeps the value from before the
  first one, so midnight always returns to the usual schedule.
- A normal call (without `today_only`) for the same weekday and platform cancels
  the pending restore of the field it sets: that value is meant to last.
- A change that still cannot be written back after 7 days (for example while
  Microsoft is unreachable) is dropped.
- Xbox allowed hours work today-only too (`platform: Xbox` on
  `set_screentime_intervals`), with the same requirements as above: the Xbox
  Family Settings sign-in linked. The kept value is the day's whole list of
  Xbox time windows.

To put the usual values back before midnight:

```yaml
action: microsoft_family_safety.restore_today_only
data:
  account_id: "1234567890123456"
  platform: Xbox  # optional; omitted = every platform
```

Pending changes are shown in the `today_only` attribute of
`sensor.{prefix}_screen_time_policy`, by platform (empty when nothing is
pending):

```yaml
today_only:
  Windows:
    date: "2026-11-11"
    restores_at: "2026-11-12T00:00:00+01:00"
    allowed_hours: [{start: "07:00", end: "23:00"}]
    restore_allowed_hours: [{start: "07:00", end: "21:00"}]
  Xbox:
    date: "2026-11-11"
    restores_at: "2026-11-12T00:00:00+01:00"
    limit_minutes: 180
    restore_limit_minutes: 120
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
# Block several websites in one call (also works for allow_website and remove_website)
service: microsoft_family_safety.block_website
data:
  account_id: "1234567890123456"
  website:
    - "example.com"
    - "example.net"
response_variable: result
```

Each site is its own request to Microsoft. The same site given twice (`example.com`, `https://example.com`, `EXAMPLE.com`) is sent once, and a list with no site left after blanks are dropped is refused. Every site in the list is tried, also after one fails; if any fails, the call fails with a message naming the sites that were done and the sites that failed (with Microsoft's error), so an automation can retry just those. The response is optional; asked for, a successful call answers the sites done:

```yaml
{"done": ["example.com", "example.net"], "failed": []}
```

```yaml
# Toggle web content filtering
service: microsoft_family_safety.toggle_web_filter
data:
  account_id: "1234567890123456"
  enabled: true
```

```yaml
# Keep the blocked websites in step with a published list (administrators only)
service: microsoft_family_safety.sync_site_list
data:
  account_id: "1234567890123456"
  url: "https://example.com/blocklist.txt"
response_variable: sync
```

`sync_site_list` reads a plain-text list over https (one domain per line, `#` comments, up to 1 MiB, redirects not followed). It blocks the listed domains that are not blocked yet and, on later runs, unblocks only the domains this same list added and has since dropped. Sites blocked by hand and allowed sites are never touched, and a domain that overlaps an allowed site is skipped. Which domains a list added is kept per child in Home Assistant's storage. An empty or failed download, a list that would take back more than half of what it added, or an answer from Microsoft without any of the sites the list added changes nothing. `stop: true` removes everything the list added and forgets it. The response gives counts and examples of `added`, `removed`, `kept`, `skipped_allowed`, `failed` and `ignored`. Run it from an automation to keep a list current, for example once a day.

```yaml
# Read the blocked and allowed websites (administrators only)
service: microsoft_family_safety.get_sites
data:
  account_id: "1234567890123456"
response_variable: sites
```

`get_sites` answers with `blocked` and `allowed` as Microsoft stores them (absolute URLs), `filter_enabled`, and `from_list` / `from_list_by_url`: the blocked domains that `sync_site_list` added, so they can be told apart from sites blocked by hand.

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

### Reauthentication

Starts the Microsoft sign-in flow immediately, without waiting for Microsoft to refuse the stored sign-in. Useful behind a dashboard button (the example card's connection pill opens the integration page for the same purpose). No fields.

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

### Warn before time runs out

```yaml
automation:
  - alias: "Five minutes of Xbox left"
    trigger:
      - platform: numeric_state
        entity_id: sensor.firstname_xbox_time_remaining
        below: 6
    condition:
      - condition: state
        entity_id: binary_sensor.firstname_xbox_allowed_now
        state: "on"
    action:
      - action: notify.mobile_app_your_phone
        data:
          message: "Five minutes of Xbox left today."
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

### Sign-in never finishes

- The authentication proxy expires **10 minutes** after it is created. If you took longer, start the flow again.
- **On a phone** (Home Assistant app or mobile browser) the Microsoft page opens in a separate browser. Once it shows *Authentication completed*, close it and switch back to Home Assistant: since 2.0.9 the dialog picks up the result within a few seconds of coming back. Before 2.0.9 it could stay stuck because Android pauses the app while the sign-in runs; signing in from a desktop browser avoids that on older versions.
- If Home Assistant aborts with *"The browser sign-in could not be loaded"* or *"The browser authentication flow expired"*, simply restart the flow.


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

- Check the **Connection** sensor's `screentime_policy_status`: `network_error` means Microsoft did not answer (it is retried at the next update), `error` that it refused the request or answered something unreadable (see the debug log), `no_policy_data` that the child has no Windows schedule.
- If Microsoft refuses the refresh token itself, every sensor stops updating and Home Assistant raises a reauthentication prompt — complete it.

### Screen time writes fail

- Writes go to the mobile API (`/v4/devicelimits/schedules`, as the Family Safety app). If reads are also `unknown`, fix that first.
- Microsoft answers the schedule writes slowly: a lock or unlock (7 days, allowance and time window each) can take about a minute.
- A partial failure raises an error naming how many of the 7 weekdays were updated, and **keeps the saved restore point** so you can safely retry.

### Account Lock issues

- **Lock refuses to run** with *"current schedule unreadable and no saved policy exists"* — this is the safety guard from issue #23. It prevents wiping a child's real schedule when the current one cannot be read and there is nothing to restore from. When Microsoft did not answer, the message says so: retry in a few minutes.
- **Unlock refuses to run** with *"no saved schedule to restore"* — the restore point is gone, typically because `.storage/microsoft_family_safety.saved_screentime.<entry_id>` was deleted. The integration will not invent a schedule; set the daily limits again from Home Assistant or at [account.microsoft.com/family](https://account.microsoft.com/family). Before 2.0.3 it wrote a 2 h/day, 07:00-22:00 default here, which silently replaced the real schedule.
- **Lock is account-wide** — it affects all platforms simultaneously.
- Lock and unlock **stop at the first network failure** rather than waiting out a timeout for each of the seven days, and keep the saved schedule so the operation can be retried.

### "Microsoft cannot resolve part of the family roster"

Microsoft's mobile API sometimes answers `Family.UnableToFindTargetResource` / `RosterError` (HTTP 404) for one child, typically when a reset or decommissioned device is still listed in the family, or when a school/work (Entra ID) account is enrolled on the child's device. Since 2.0.7 the integration keeps running: the affected child keeps empty values for the data Microsoft refuses (devices, screen-time usage, apps, lockable platforms or spending), everything else loads normally, and a persistent notification plus the `roster_errors` attribute of the Connection sensor tell you which member and which data are affected. Removing the stale device at https://account.microsoft.com/family clears it when that is possible; a school-managed device usually cannot be removed, in which case the degraded data for that child is expected.

### Diagnostics

**Settings > Devices & services > Microsoft Family Safety > ⋮ > Download diagnostics** gives the entry options, the connection state (as on the connection sensor) and the last polled data, each app list reduced to a count. Tokens, cookies, names, pictures, device names, e-mail addresses, the account balance, the website lists and the websites visited are removed, and account, device and Xbox user ids are replaced by `account_1`, `device_1`, `xbox_user_1` (the parent's ids by `parent`, `xbox_parent`). Attach it to an issue together with a debug log.

### Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Useful markers: `Native Microsoft Family mobile OAuth redirect captured` at the end of the browser sign-in; `Xbox Family Settings linked from the sign-in` or `could not be linked from the sign-in` right after it.

---

## Security & limitations

### Security

Be aware of the following before enabling native authentication:

- **Credentials are stored in clear text.** The Family Safety and Xbox Family Settings refresh tokens are persisted in the config entry and in `.storage/` **unencrypted**. No browser cookies are stored. Anyone with read access to your Home Assistant configuration directory (including backups) can extract a usable Microsoft token. Protect and encrypt your backups accordingly.
- **The authentication proxy endpoint is unauthenticated.** `/auth/microsoft_family_safety/proxy/{token}` and its callback are registered without Home Assistant authentication — the only protection is a 24-byte random token in the URL. The flow is scoped: the proxy only serves the Microsoft sign-in hosts (`account.microsoft.com`, `account.live.com`, `login.live.com`, `login.microsoft.com`, `login.microsoftonline.com`), the callback only accepts that token for its own flow, it exists for at most **10 minutes**, and it is destroyed when the flow finishes. Still, while a flow is live, anyone who can reach your Home Assistant HTTP interface **and** guess the token would be proxied to Microsoft.
- **The Microsoft login page is served from your Home Assistant origin.** Because the sign-in is proxied, the address bar shows your Home Assistant URL rather than `login.live.com`, so you cannot verify the Microsoft certificate visually. Only start the flow from Home Assistant itself.
- **HTTP mode is genuinely insecure.** With `allow_insecure_http_auth` enabled, your Microsoft password crosses the local network unencrypted. It is restricted to local/private addresses and logs a warning, but it remains a testing option, not a deployment mode.

### Limitations

- **Unofficial API** — Microsoft provides no public API for Family Safety. This integration relies on reverse-engineered endpoints that may change or break at any time.
- **Renewal depends on the refresh tokens.** The integration renews and stores Microsoft's token rotations by itself. It cannot recover from a revoked login (password change, security review); Home Assistant will prompt you then.
- **A browser is required to sign in.** Microsoft's OAuth sign-in is interactive (password, MFA). There is no headless sign-in.
- **Entity IDs changed** — see [Breaking changes](#breaking-changes).
- **Per-platform lock can answer an error** — Microsoft's Windows/Mobile override endpoint sometimes answers HTTP 500. The integration reads the overrides back after each lock or unlock and only reports an error when the change really did not apply.

---

## API Reference

### Mobile API — `mobileaggregator.family.microsoft.com`

**Authentication:** OAuth token of the Family Safety app (client `000000000004893A`), refreshed from the stored refresh token. The roster, devices, usage, applications and requests go through `pyfamilysafety`; the integration adds:

| Method | Endpoint | Description | Used by |
|--------|----------|-------------|---------|
| GET / PATCH | `/v4/devicelimits/schedules/{childId}` | Windows and Xbox schedules (allowance and time windows per day, on/off) | `sensor.screen_time_policy`, `number.*_limit`, `time.*_start/end`, `switch.lock`, `switch.screen_time_limits`, `switch.xbox_screen_time_limits` |
| GET / PATCH | `/v1/WebRestrictions/{childId}` | Web filter and the blocked / allowed sites (`exceptions=true`); block/allow/remove a website (one operation per site), web filter on/off | `sensor.web_filter`, `switch.web_filtering`, website services, `get_sites`, `sync_site_list` |
| GET / PATCH | `/v1/ContentRestrictions/{childId}` | Age rating & ask-to-buy | `sensor.web_filter` attributes, services |
| GET / PATCH | `/v2/AppLimits/settings/{childId}` | "App, feature, and game limits" switch | `switch.app_limits` |
| PATCH | `/v3/AppLimits/policies/{childId}/{appId}` | Per-app time limits | app time limit services |
| GET | `/v1/activityreport/webactivity/{childId}` | Websites visited in Edge (`allowStatus=true`, 403 when there is no data) | `sensor.websites_visited` |
| GET | `/v1/activityreport/searchactivity/{childId}` | Bing searches | `sensor.websites_visited` attributes |
| GET | `/v4/devicelimits/{childId}/overrides` (`Plat-Info: ALL`) | Active device overrides, read fresh on every poll and around every lock/unlock; the platform name to send is its `appliesTo` (Windows can be listed as `Desktop`) | `switch.lock`, `lock_platform` / `unlock_platform` |
| POST | `/v4/devicelimits/{childId}/overrides` (`Plat-Info: ALL`) | Lock (`BlockUntil`) or unlock (`Cancel`) Windows/Mobile; body `{target, overrideType, validUntil, culture}` | `lock_platform`, `unlock_platform` |

> The POST answers with the whole overrides document as it stands after the write, and `lock_platform` / `unlock_platform` / Windows `add_time_bonus` / `cancel_time_bonus` are judged by that answer: a GET made right after a write can still list the previous state for a while. Microsoft sometimes answers HTTP 500 to a write it applied anyway, and has failed a cancel without applying it; after an error, a timeout or an answer without the document, the overrides are read up to three times over a few seconds, and the action succeeds only when a read shows the expected state; when no read succeeds it fails. A poll answer without the platform list keeps the last known locks. For two minutes after a write, its answer stands over what a GET says for that platform (the poll, the lock holds, strict mode), so a lagging read is never taken for a parent's change.

### Xbox Family Settings — `xapprove.xboxlive.com`

**Authentication:** refresh token of the Xbox Family Settings app (client `00000000482c8f49`), exchanged for an Xbox Live XSTS token plus an `x-ms-rps-ticket`. Children are found on the Xbox family roster (`accounts.xboxlive.com`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET / PATCH | `/api/v1/user/{xuid}/settings/screentime` | Xbox schedule the console enforces |
| GET | `/api/v1/user/{xuid}/settings/screentime/overrides` | Active Xbox lock / extra time |
| POST | `/api/v1/user/{xuid}/settings/screentime/overrides/create` | Lock the Xbox (type 2) or release it (type 100) |
| GET | `/api/v1/user/{xuid}/activityreport/screenTimeUsage` | Xbox play time |

### Home Assistant authentication proxy

Registered by the integration only while a sign-in flow is running, and destroyed when it finishes or after 10 minutes. Both routes are served **without Home Assistant authentication**; the random per-flow token in the URL is the only guard. See [Security & limitations](#security--limitations).

| Method | Endpoint | Description |
|--------|----------|-------------|
| any | `/auth/microsoft_family_safety/proxy/{token}[/{path}]` | Reverse proxy to Microsoft sign-in (restricted to `account.microsoft.com`, `account.live.com`, `login.live.com`, `login.microsoft.com`, `login.microsoftonline.com`) |
| GET | `/auth/microsoft_family_safety/callback?flow_id=` | Hands the captured result back to the config flow |

---

## Contributing

Contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes and open a pull request

Areas where help is especially appreciated:
- Microsoft API endpoint documentation and analysis
- Encrypting the stored Microsoft tokens at rest
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

When reporting an issue, please include: HA version, integration version, the attributes of the **Connection** sensor, debug logs, and steps to reproduce.

[releases-shield]: https://img.shields.io/github/release/noiwid/HAFamilySafety.svg?style=for-the-badge
[releases]: https://github.com/noiwid/HAFamilySafety/releases
[license-shield]: https://img.shields.io/github/license/noiwid/HAFamilySafety.svg?style=for-the-badge
[license]: LICENSE
[marketplace-shield]: https://img.shields.io/badge/Marketplace-Default-41BDF5.svg?style=for-the-badge
[marketplace]: https://my.home-assistant.io/redirect/hacs_repository/?owner=noiwid&repository=HAFamilySafety&category=integration
