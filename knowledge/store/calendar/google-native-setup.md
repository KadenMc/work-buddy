---
name: Google Calendar native (OAuth) setup
kind: directions
description: Step-by-step setup for the google_native calendar provider — Google Cloud project, Desktop-app OAuth client, where to put the client secret, the one-time consent flow, and switching the provider over.
trigger: User wants to set up direct (own-OAuth) Google Calendar access, or /wb-setup flags the google_calendar_native component as not configured.
tags:
- calendar
- google-calendar
- oauth
- setup
- credentials
aliases:
- google calendar oauth setup
- google native setup
- set up google calendar api
- calendar oauth
parents:
- calendar
---

How to set up `provider: google_native` — direct Google Calendar access over its
v3 REST API with your own OAuth credentials, with **no Obsidian dependency**.
This is the durable path that eventually replaces the Obsidian bridge for Google.
The default provider stays `obsidian_bridge` until you've validated native and
flip it deliberately.

Two files live under the gitignored data root (`<repo>/.data/credentials/`) —
**never commit either**. They are different things, and conflating them is the
#1 point of confusion:

- **`google_client_secret.json`** — the app's **"ID badge."** It identifies *the
  work-buddy app* to Google. You download it once from Google Cloud. It **never
  expires**, and Testing-vs-Production has nothing to do with it.
- **`google_oauth_token.json`** — *your* **"signed permission slip"** ("I let
  this app see/edit my calendar"). The **browser consent pop-up** creates it.
  This is the only file whose durability depends on Testing vs Production (below).

## Gotchas this guide exists to prevent

- **Two permissions, not one.** Listing your calendars is a *separate* Google
  permission from reading/writing events, so the consent screen asks for **two**
  scopes (`calendar.events` + `calendar.calendarlist.readonly`). A token with
  only `calendar.events` fails with "insufficient authentication scopes" the
  moment it tries to list calendars.
- **The "Publish" button is hidden** under the **Audience** sub-tab (step 2).
- **`poetry install` won't update the conda env** by default (step 5).
- **The badge doesn't expire; only the permission slip can** — and only if you
  consented while the app was still in "Testing" (step 2). Publish to Production
  *before* you run the consent flow.

## 1. Google Cloud project + Calendar API

1. Go to <https://console.cloud.google.com/> and **create a project** (e.g.
   named `work-buddy`).
2. **Enable the Google Calendar API**: APIs & Services → Library → search
   "Google Calendar API" → Enable.

## 2. OAuth consent screen — PUBLISH TO PRODUCTION

APIs & Services → OAuth consent screen (newer console: **Google Auth Platform**).

1. User type: **External** (a personal Gmail can't use "Internal"; that needs a
   Workspace org). Fill in the app name, your support email, and developer
   contact. The scopes are requested by the app at consent time, so you don't
   have to pre-add them here.
2. **Publish the app to "Production".** The publishing status lives under the
   **Audience** sub-tab (OAuth consent screen → Audience, or
   <https://console.cloud.google.com/auth/audience>): find **Publishing status:
   Testing** and click **PUBLISH APP**. This is important: in **"Testing"** mode,
   refresh tokens for sensitive scopes (Calendar is sensitive) **expire after ~7
   days** — you'd have to re-consent every week, and you must also add your email
   under **Test users**. Published-to-Production refresh tokens are durable. Since
   the app
   stays private to you, no Google verification review is required for personal
   use; you'll click through an "unverified app" warning once during consent.

## 3. Create a Desktop-app OAuth client + download the secret

APIs & Services → Credentials → **Create Credentials → OAuth client ID**.

1. Application type: **Desktop app**. Name it anything. Create.
2. **Download the JSON** (the "Download JSON" button on the client). Google names
   it `client_secret_<long-id>.apps.googleusercontent.com.json`.

## 4. Put the secret where work-buddy finds it automatically

Save the downloaded JSON as:

```
<repo>/.data/credentials/google_client_secret.json
```

(`.data/` is the data root — gitignored.) It is **auto-discovered** there; no env
var or config is needed. Two ways to put it there:

- **Manually:** rename the downloaded file to `google_client_secret.json` and
  move it into `.data/credentials/`.
- **Via the wizard:** `/wb-setup` → diagnose `google_calendar_native` → run the
  fix for the client-secret requirement and give it the path to your download —
  it validates the file is a Desktop-app secret and copies it into place.

(Override only if you must: set the `GOOGLE_OAUTH_CLIENT_SECRET` env var to a
path, or `calendar.google_native.client_secret_path` in config. The convention
path wins when present.)

## 5. One-time consent flow → writes the refresh token

This opens a browser; you click through the "unverified app" screen, then approve
two permissions — **"See and edit events on your calendars"** (`calendar.events`)
and **"See the list of calendars you're subscribed to"**
(`calendar.calendarlist.readonly`). The refresh token is then saved to
`.data/credentials/google_oauth_token.json`.

**Dependency gotcha:** the interactive flow needs `google-auth-oauthlib`. It is in
`pyproject.toml`/`poetry.lock`, but **Poetry installs into its own venv, not the
conda env** (a classic conda+Poetry seam). So `conda run -n work-buddy python …`
will fail with `ModuleNotFoundError: google_auth_oauthlib`. Two ways to run the
flow:

- **Via Poetry's venv (no env surgery — easiest):** it already has the lib.
  ```bash
  poetry run python -c "from work_buddy.calendar import google_auth; print(google_auth.run_oauth_flow({}))"
  ```
- **Via the wizard** (`/wb-setup` → diagnose `google_calendar_native` → fix the
  token requirement): the fixer runs inside the sidecar (the conda env), so the
  conda env must have the lib first. Sync it once with
  `poetry config virtualenvs.create false` then `conda activate work-buddy &&
  poetry install` (this installs into the active conda env; re-apply the CUDA
  torch override afterward if you use it — see the README).

Only the *interactive flow* needs `google-auth-oauthlib`. Steady-state runtime
(load token + silent refresh) needs only `google-auth`, which the conda env
already has — so day-to-day use never imports oauthlib.

Note: only the *interactive flow* needs `google-auth-oauthlib`. The steady-state
runtime (load token + silent refresh) needs only `google-auth`, which is already
present — so once the token exists, day-to-day use never imports oauthlib.

## 6. Switch the provider (after validating parity)

`google_native` value-adds robustness (no dependency on Obsidian being open) and
real iCalUIDs (proper cross-calendar dedup), not new day-one capability — the
bridge already covers your Google calendars. So validate first:

1. Confirm `/wb-setup` shows `google_calendar_native` healthy (client secret +
   token present, API responds).
2. Point `calendar.provider: google_native` in `config.yaml`, **restart the
   sidecar**, and sanity-check `calendar_coverage` matches the bridge snapshot.
3. Keep the bridge in-tree as a fallback for environments without OAuth.

## Troubleshooting

- **"no OAuth token" / token invalid** → re-run the consent flow (step 5).
- **Token stopped refreshing after ~a week** → your consent screen is still in
  "Testing"; publish it to Production (step 2) and re-run consent.
- **`google-auth-oauthlib` missing** → sync the runtime env
  (`conda activate work-buddy && poetry install`); it's already in the lockfile.
- The OAuth client secret exposed in the Obsidian plugin's `data.json` is **not**
  reusable — `google_native` needs its own Google Cloud client (this setup).
