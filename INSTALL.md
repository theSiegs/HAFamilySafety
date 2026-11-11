# Microsoft Family Safety - Installation Guide

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=noiwid&repository=HAFamilySafety&category=Integration)

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [Marketplace (Recommended)](#marketplace-recommended)
  - [Manual Installation](#manual-installation)
- [Configuration](#configuration)
- [Options](#options)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [Security & Privacy](#security--privacy)

---

## Prerequisites

- **Home Assistant** 2024.1.0 or newer (any installation type: OS, Supervised, Container, Core)
- A Home Assistant URL reachable from your browser over **HTTPS**. If your instance is HTTP-only on the local network, see [Insecure local HTTP](#insecure-local-http)
- The **Marketplace** -- recommended. It is built into Home Assistant since 2026.11; before that it is a separate install called HACS ([install guide](https://hacs.xyz/docs/setup/download))
- **Microsoft account** with parent/organizer role in a Family Safety group, with at least one child account and monitored device
- **Python dependencies:** `pyfamilysafety==1.1.2` (installed automatically)

> **The Playwright auth add-on is no longer used.** Authentication happens natively inside Home Assistant, and after the sign-in the integration runs on refresh tokens only. Entries created with the add-on keep working; the add-on can be uninstalled.

> **Upgrading from an add-on based setup?** Re-adding the integration creates new entity IDs. Read the *Breaking changes* section of the [README](README.md#breaking-changes) first.

---

## Installation

### Marketplace (Recommended)

1. Go to **Settings > Marketplace > Browse**, search for **Microsoft Family Safety**, and install it.
2. Restart Home Assistant: **Settings > System > Restart**.

### Manual Installation

1. Download the [latest release](https://github.com/noiwid/HAFamilySafety/releases) and extract it.
2. Copy the `custom_components/microsoft_family_safety/` folder into your Home Assistant `config/custom_components/` directory.

   Expected directory structure (main files):

   ```
   config/
   └── custom_components/
       └── microsoft_family_safety/
           ├── __init__.py
           ├── _pyfamilysafety_compat.py
           ├── api_client.py
           ├── auth/
           │   └── native_proxy.py
           ├── button.py
           ├── config_flow.py
           ├── const.py
           ├── coordinator.py
           ├── manifest.json
           ├── number.py
           ├── sensor.py
           ├── services.yaml
           ├── strings.json
           ├── switch.py
           ├── time.py
           └── translations/
               ├── de.json
               ├── en.json
               └── fr.json
   ```

3. Restart Home Assistant: **Settings > System > Restart**.

---

## Configuration

### Step 1 -- Add the Integration

1. Go to **Settings > Devices & Services**.
2. Click **+ Add Integration** and search for **Microsoft Family Safety**.

### Step 2 -- Set the Basic Options

The first dialog asks for:

| Field | Default | Notes |
|-------|---------|-------|
| Update interval (seconds) | 300 | 30 -- 3600 |
| Monitored platforms | Windows | Windows / Xbox / Mobile. Tick Xbox only if the children use one, see [below](#xbox-features) |
| Allow insecure local HTTP authentication (testing only) | off | See [below](#insecure-local-http) |

Click **Submit**.

<a id="xbox-features"></a>
#### Xbox

The Xbox lock, the Xbox limits and Xbox play time go through the **Xbox Family Settings** app's service, on its own sign-in. Everything else (schedules, account lock, app limits, websites, age rating, requests) runs on the mobile token, like the Family Safety app.

- **Xbox ticked**: the Xbox entities are created, and the sign-in below also tries to link the Xbox Family Settings sign-in.
- **Xbox unticked** (default): no Xbox entities are created.

You can change it later under **Configure**, where the Xbox Family Settings sign-in can also be linked by hand (**Link the Xbox Family Settings sign-in**).

### Step 3 -- Sign In to Microsoft

Home Assistant shows an **Open website** button.

1. Click it. A browser window opens on the Microsoft sign-in page, served through a temporary authentication proxy that Home Assistant mounts inside its own HTTP server.
2. Sign in with your **parent/organizer** Microsoft account (not a child account) and complete MFA if prompted.

You do **not** copy an authorization URL and you do **not** paste a redirect URL back.

### Step 4 -- Let Home Assistant Finish

As soon as Microsoft hands back the authorization code, the window closes itself and Home Assistant exchanges the code for the mobile refresh token. With Xbox ticked it then asks Microsoft, with the cookies of that same sign-in, for a code of the Xbox Family Settings app; when Microsoft answers without another sign-in page, the Xbox link is made with no extra step. Otherwise the Connection sensor shows `xbox_family: not_linked`: link it from **Configure**. The sign-in cookies are not kept.

### Step 5 -- Finish

The integration discovers family members and devices automatically. The first data pull happens on the next update cycle, so entities can read `unknown` for **up to one update interval (5 minutes by default)** right after setup before the first schedule and usage values appear. That too is expected.

---

## Options

**Settings > Devices & Services > Microsoft Family Safety > Configure**:

| Option | Range / Format | Default |
|--------|----------------|---------|
| Update interval (seconds) | 30 -- 3600 | 300 |
| Monitored platforms | Windows / Xbox / Mobile | Windows |
| Allow insecure local HTTP authentication (testing only) | on / off | off |
| Link the Xbox Family Settings sign-in | on / off | off |

Changes take effect immediately.

### Insecure local HTTP

Native authentication proxies your Microsoft credentials through Home Assistant, so it requires an **HTTPS** Home Assistant URL by default. If Home Assistant is only reachable over plain HTTP, you may tick **Allow insecure local HTTP authentication (testing only)**.

It is accepted **only** for `localhost`, `homeassistant`, `*.local`, or a private / loopback / link-local IP address. A public HTTP URL is refused with:

> Insecure HTTP authentication is allowed only through localhost, .local, or a private/local IP address.

While enabled, your Microsoft password and session data travel **unencrypted across your local network**, and a WARNING is logged for every sign-in. Treat it as a testing option; configuring HTTPS is the better answer.

### HTTP-only instances

Without an HTTPS URL and without the local HTTP option, the setup offers the manual step instead: open the Microsoft link shown, sign in, and paste the address of the blank page you end up on.

---

## Verification

1. Go to **Settings > Devices & Services** and click on **Microsoft Family Safety**.
2. Confirm devices and entities have been created.
3. Check the **Connection** diagnostic sensor. It should read `connected`, with `screentime_policy_status: ok`. With Xbox ticked, `xbox_family: ok` (or `linked`) shows the Xbox Family Settings sign-in is in use. `degraded` means Microsoft refused that Xbox sign-in: only the Xbox features are affected.
4. Go to **Settings > Devices & Services > Entities** and search for your child's name. You should see sensors, switches, buttons, 7 number entities (daily limits) and 14 time entities (7 start + 7 end).
5. If entities show "unavailable," wait up to one update interval (default 5 minutes) for the first data pull.

> Entity IDs are prefixed with the device name, e.g. `number.firstname_lastname_family_safety_firstname_sunday_limit`. See the [README](README.md#breaking-changes).

---

## Troubleshooting

### "Automatic browser authentication normally requires HTTPS"

No HTTPS Home Assistant URL is configured. Configure HTTPS, or enable [Insecure local HTTP](#insecure-local-http).

### The sign-in window returns "400 Bad Request"

Home Assistant's `security_filter` middleware rejects query strings matching a file-injection pattern, and some of Microsoft's silent-SSO redirects (`epctrc=/w/...`) match it. The integration carries such a query encoded through Home Assistant and restores it before it reaches Microsoft. If you still see the error, retry the flow and check the logs for `security_filter` entries.

### "Microsoft authentication host not allowed"

Fixed in 2.0.4: Microsoft's sign-in page links some pages with an explicit port (`login.microsoftonline.com:443`) and older versions rejected it. Update the integration and retry.

### Sign-in never completes

- The authentication proxy expires after **10 minutes**. If you took longer, restart the flow.
- On a phone, once the Microsoft page shows *Authentication completed*, close it and switch back to Home Assistant; since 2.0.9 the dialog continues on its own within a few seconds. On older versions, sign in from a desktop browser instead.
- *"The browser authentication flow expired. Please start again."* -- restart the flow.


### "502 Bad Gateway" from nginx during sign-in

nginx logs `upstream sent too big header while reading response header from upstream`. Microsoft's sign-in pages answer with large cookie headers, and the integration's sign-in proxy passes them on, which overflows nginx's default response-header buffer (4 or 8 KB). Raise it in the `location` that proxies Home Assistant:

```nginx
proxy_buffer_size       128k;
proxy_buffers           4 256k;
proxy_busy_buffers_size 256k;
```

Nginx Proxy Manager: add the same three lines under the host's **Advanced** tab. Then restart the sign-in.

Official "NGINX Home Assistant SSL proxy" add-on: in the add-on configuration, enable **Customize** > `active`, keep the default file names (`nginx_proxy_default*.conf`), put the same three lines in `/share/nginx_proxy_default_proxy_buffer.conf` and restart the add-on.

### Wrong account

*"A different Microsoft account was used."* -- reauthentication must use the same Microsoft account the entry was created with.

### No Entities Appear

- Verify your Family Safety setup at [account.microsoft.com/family](https://account.microsoft.com/family) -- at least one child and one monitored device must exist.
- Wait at least one full update interval after setup.
- Check **Settings > System > Logs** for entries containing `microsoft_family_safety`.
- Reload the integration: three-dot menu > **Reload**.

### Session Expired / Reauthentication Required

The integration renews its refresh tokens by itself. You are only asked to sign in again when Microsoft refuses the mobile refresh token (password change, security review, revoked sign-in) or the Xbox Family Settings sign-in.

1. Go to **Settings > Devices & Services**.
2. Find **Microsoft Family Safety** -- it shows a **Reauthenticate** button. You can also call the `microsoft_family_safety.request_reauth` service to start the flow without waiting, for example from a dashboard button.
3. Follow the same sign-in steps. The mobile refresh token is renewed and, with Xbox ticked, a missing or refused Xbox Family Settings link is made again.

### Dashboard card

A ready-made per-child card ships in [`examples/family-safety-card.yaml`](examples/family-safety-card.yaml) (decluttering template) with a full example view in [`examples/dashboard.yaml`](examples/dashboard.yaml). It needs the Marketplace dashboard cards `decluttering-card`, `button-card`, `vertical-stack-in-card`, `card-mod` and `mushroom`. See the README section *Dashboard card* for the variables to fill in.

### Debug Logging

Add the following to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Check logs at **Settings > System > Logs**. Include these logs when filing [issues on GitHub](https://github.com/noiwid/HAFamilySafety/issues).

---

## Security & Privacy

- **OAuth 2.0 and browser sign-in:** Authentication uses Microsoft's own sign-in pages. Your password is never stored, never logged, and never persisted by the integration.
- **The sign-in is proxied through Home Assistant.** Microsoft's login page is served from your Home Assistant origin, so the address bar shows your Home Assistant URL rather than `login.live.com` and you cannot check the Microsoft certificate visually. Only ever start the flow from Home Assistant itself.
- **Stored credentials are not encrypted.** The Family Safety and Xbox Family Settings refresh tokens are written to the config entry and to `.storage/` in **clear text**; no browser cookies are stored. Anyone able to read your configuration directory -- including anyone holding an unencrypted backup -- can extract a usable Microsoft token. Protect and encrypt your backups.
- **The authentication proxy endpoint is unauthenticated.** `/auth/microsoft_family_safety/proxy/{token}` and its callback are registered without Home Assistant authentication; the only guard is a 24-byte random token in the URL. It only serves the Microsoft sign-in hosts, the callback only accepts that token for its own flow, it lives at most **10 minutes**, and it is destroyed when the flow ends.
- **Scope:** The integration only accesses Microsoft Family Safety data -- no access to email, files, or other services.
- **Unofficial API:** This integration uses unofficial Microsoft Family Safety APIs. Use at your own risk.
- **Local processing:** All data processing happens within your Home Assistant instance. Data is fetched from Microsoft servers; only tokens are persisted locally.
