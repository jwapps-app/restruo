# Security

## Reporting a vulnerability

Please open a [security advisory](https://github.com/jwapps-app/restruo/security/advisories/new)
rather than a public issue. I maintain this in my spare time, so expect a reply in days
rather than hours.

## What Restruo holds

Restruo stores the credentials for each Portainer instance you add — an API token or a
username/password — in `/data/instances.json` on its volume. They are:

- written with `0600` permissions
- never returned by the API (the instance list omits secrets)
- never logged
- used only to call the Portainer instance you entered them for

Anyone with access to that volume, or to the container's filesystem, can read them.

## Deployment expectations

Restruo is built for a trusted LAN. It is not hardened for direct exposure to the
internet, and it can redeploy every stack on every machine you connect to it.

- **Keep dashboard auth enabled** (it is by default; it requires `DASHBOARD_PASSWORD`).
- **Don't port-forward it.** For remote access use a VPN, Tailscale, or an authenticated
  reverse proxy.
- **Scope the Portainer credential.** It has the full power of the account it belongs to.
  Prefer an API token over a password, and a least-privileged user if your Portainer
  edition supports RBAC.
- `verify_tls: false` disables certificate verification for that instance. It exists
  because self-signed certs are common in home labs — prefer real certificates.

## Known limits

- Sessions are stateless signed cookies (30 days), signed with a key derived from the
  dashboard password. Signing out clears the cookie on that device. To sign out everywhere,
  change `DASHBOARD_PASSWORD` (or delete `session_secret` from the data volume) and
  restart.
- Ten failed logins from one address block that address for fifteen minutes, on the login
  form and on basic auth alike. Each failure is logged with its source address.
- Browser sessions must send `X-Restruo: 1` on any request that changes something; the
  page does. It stops another page on the same host from reusing the session cookie.
- The container starts as root only to make its data volume writable by the unprivileged
  `restruo` user (uid 1000), then drops privileges before the app runs.
- There is no per-user access control — one dashboard login, full access.
