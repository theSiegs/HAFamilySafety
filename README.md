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
| **Screen time policies** | Adjust daily allowances and allowed time intervals per day — directly from the UI |
| **App management** | Block/unblock apps, set per-app time limits and windows |
| **Web filtering** | Block/unblock domains, toggle content filtering, set PEGI age ratings |
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
| Allow insecure local HTTP authentication (testing only) | on / off | off | See below |
| Link the Xbox Family Settings sign-in | on / off | off | Only with Xbox ticked; for when the Connection sensor shows `xbox_family: not_linked`. Opens a Microsoft sign-in through the same temporary proxy as the setup, which picks up the result itself: sign in, close the window, done. Without an HTTPS URL (or the local HTTP option below), or if the sign-in does not finish within 5 minutes, you paste the address of the blank page Microsoft ends on instead |

#### Who can control the integration

Home Assistant lets every user press any button, flip any switch or change any number, unless an entity permission policy says otherwise, and Home Assistant has no screen to write such policies. If your child has a Home Assistant account, they could otherwise lift their own limits from the integration's entities.

The option **Users allowed to control Microsoft Family Safety** closes that gap:

- **Empty** (default): nothing changes, every user can use the buttons and settings.
- **Some users ticked**: only they and the administrators can. Any other account is refused, with a warning in the log, on every button, switch, daily limit and time window of the integration, and on every `microsoft_family_safety.*` action.
- Automations and scripts started by Home Assistant itself are never affected. A script started by a person from the dashboard runs with that person's rights.

Give the child a **non-administrator** account and tick the parents: an administrator can change this option, so a child with an administrator account bypasses it. The child still sees the entities (Home Assistant cannot hide them per user) but can no longer use them. Changing the option takes effect immediately, without reloading the integration.

#### Xbox

The Xbox lock, the Xbox limits and Xbox play time go through the **Xbox Family Settings** app's service, on its own refresh token. Microsoft moved the Xbox settings there: its per-day limits are the ones the console enforces, and an Xbox override stored by the Family Safety mobile API is accepted but ignored by the console.

- **Xbox ticked** among the monitored platforms: the Xbox entities are created (Xbox lock, Xbox schedule switch, Xbox daily limits). The Xbox Family Settings sign-in is linked during the Microsoft sign-in when Microsoft allows it. Otherwise a repair issue appears (and the Connection sensor shows `xbox_family: not_linked`): open the options, tick **Link the Xbox Family Settings sign-in** and sign in with the parent account in the window that opens (without an HTTPS URL, open the link shown instead and paste the address of the blank page you end up on). If Microsoft later refuses that sign-in, it is retried every 6 hours and a repair issue asks you to link it again the same way.
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
| `sensor` | 7 – 8 | Screen Time, Account Info, Applications, Pending Requests, Web Filter, Websites Visited, Screen Time Policy (+ Balance when the account exposes one) |
| `switch` | 4 + platforms + apps | Account Lock, Screen Time Limits, Xbox Screen Time Limits, App Limits, one Platform Lock per monitored platform, one per application |
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
| Websites Visited | `sensor.{prefix}_websites_visited` | Visits in Edge over the last 7 days | `report_available`, `site_count`, `blocked_count`, `sites` (50 most visited: `domain`, `visits`, `last_visit`, `blocked`), `searches` (50 latest search terms, one row per term: `search`, `visits`, `date`, `site`). Read every 30 min; `report_available: false` when Microsoft has no Edge data for the child. `sites` and `searches` are not stored in the history database |
| Screen Time Policy | `sensor.{prefix}_screen_time_policy` | enabled / disabled / unknown | `monday_allowance` … `sunday_allowance`, `*_allowed_intervals`, `daily_restrictions` |

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
| Xbox Screen Time Limits | `switch.{prefix}_xbox_screen_time_limits` | Turns the child's separate Xbox schedule on or off (the days are kept). Once it is on, seven `number.{prefix}_xbox_{day}_limit` entities set the daily Xbox allowance the console enforces. Not created without Xbox among the monitored platforms. |
| App Limits | `switch.{prefix}_app_limits` | The "App, feature, and game limits" toggle above the app list. While it is OFF, the per-app time limits are kept but not enforced. |
| App Block | `switch.{prefix}_app_{appname}` | ON = app blocked. One switch per application. |
| Platform Lock | `switch.{prefix}_{platform}_lock` | ON = platform locked. Windows and Mobile are locked through the mobile API's device override, the Xbox through the [Xbox Family Settings](#xbox) sign-in (an error without it). Use **Account Lock** to lock every platform at once. |

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

### Events

| Event | When | Data |
|-------|------|------|
| `microsoft_family_safety_request` | A new pending request (more time, an app) appears. Checked on every update; the first update after a start only sets the baseline, and a request must be missing for three updates in a row before it counts as new again | `entry_id`, `account_id`, `first_name`, `request_id`, `type`, `platform`, `app_id`, `app_name`, `requested_time` (as Microsoft gives it) |

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

The integration exposes **18 services**, split between the pyfamilysafety library and the integration's own mobile API calls, plus one to start the sign-in flow on demand.

Every service that acts on one child takes the child either as `account_id` (the Microsoft user ID, shown in the `user_id` attribute of the child's entities) or as `entity_id`: any Microsoft Family Safety entity of that child or of one of its devices. The account is looked up from the entity, so automations need no hard-coded ID. A list of entities is accepted as long as they all belong to the same child. When both are given, `account_id` wins.

```yaml
# Same as account_id: "1234567890123456", read from the child's lock switch
service: microsoft_family_safety.lock_account
data:
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
  duration_hours: 24
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

**Settings > Devices & services > Microsoft Family Safety > ⋮ > Download diagnostics** gives the entry options, the connection state (as on the connection sensor) and the last polled data, each app list reduced to a count. Tokens, cookies, names, pictures, device names, e-mail addresses, the account balance, the website lists and the websites visited are removed, and account, device and Xbox user ids are replaced by `account_1`, `device_1`, `xbox_user_1`. Attach it to an issue together with a debug log.

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
- **Per-platform lock is unreliable** — Microsoft removed the `override_device` endpoint. Account Lock is the recommended replacement, but it locks all platforms at once.

---

## API Reference

### Mobile API — `mobileaggregator.family.microsoft.com`

**Authentication:** OAuth token of the Family Safety app (client `000000000004893A`), refreshed from the stored refresh token. The roster, devices, usage, applications, device overrides and requests go through `pyfamilysafety`; the integration adds:

| Method | Endpoint | Description | Used by |
|--------|----------|-------------|---------|
| GET / PATCH | `/v4/devicelimits/schedules/{childId}` | Windows and Xbox schedules (allowance and time windows per day, on/off) | `sensor.screen_time_policy`, `number.*_limit`, `time.*_start/end`, `switch.lock`, `switch.screen_time_limits`, `switch.xbox_screen_time_limits` |
| GET / PATCH | `/v1/WebRestrictions/{childId}` | Web filter and the blocked / allowed sites (`exceptions=true`) | `sensor.web_filter`, website services |
| GET / PATCH | `/v1/ContentRestrictions/{childId}` | Age rating & ask-to-buy | `sensor.web_filter` attributes, services |
| GET / PATCH | `/v2/AppLimits/settings/{childId}` | "App, feature, and game limits" switch | `switch.app_limits` |
| PATCH | `/v3/AppLimits/policies/{childId}/{appId}` | Per-app time limits | app time limit services |
| GET | `/v1/activityreport/webactivity/{childId}` | Websites visited in Edge (`allowStatus=true`, 403 when there is no data) | `sensor.websites_visited` |
| GET | `/v1/activityreport/searchactivity/{childId}` | Bing searches | `sensor.websites_visited` attributes |

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
