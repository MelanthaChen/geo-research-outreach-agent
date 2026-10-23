# Outreach administrator authentication

The dashboard is private by default in hosted environments. The browser uses
an opaque server-side session cookie (`geo_outreach_session`) and a separate
readable CSRF cookie (`geo_outreach_csrf`). Sessions expire after seven days
and are revocable from the sign-out action. Passwords are verified with
Argon2id; no password or bearer token is placed in the frontend bundle,
URL, localStorage, or sessionStorage.

## Initialize the one administrator

Run this locally from the repository, choosing the password interactively so
it is never passed on a command line or pasted into chat:

```bash
uv run python scripts/init_admin_password.py \
  --output /Users/melc/.config/geo-research-outreach-agent/admin-password-hash
chmod 600 /Users/melc/.config/geo-research-outreach-agent/admin-password-hash
```

Set the file's single Argon2id line as `OUTREACH_ADMIN_PASSWORD_HASH` in the
Render service's secret environment settings. Set
`OUTREACH_ADMIN_EMAIL=chen.yuxuan9@northeastern.edu` and
`OUTREACH_COOKIE_SECURE=true`. Do not commit the hash file. A deployment with
no hash deliberately shows a configuration error and does not silently fall
back to an unauthenticated dashboard.

`OUTREACH_DASHBOARD_TOKEN` remains supported for existing service-to-service
and operator automation clients. The browser login does not read or store
that token. All dashboard API routes require either a valid server session or
the existing machine bearer credential; `/health`, the login/session routes,
and the public questionnaire submission route remain available for their
existing purposes.
