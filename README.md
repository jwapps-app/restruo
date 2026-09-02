<img src="web/icon.svg" alt="" width="72" align="left" hspace="12">

# Restruo

**One dashboard for all your Portainer instances, with a one-click repull + redeploy per
stack.** It replaces the per-stack ritual — log in → open stack → Editor → Update the
stack → tick *Re-pull image* → Deploy — and it replaces doing that on every machine
separately.

[![build](https://github.com/jwapps-app/restruo/actions/workflows/docker.yml/badge.svg)](https://github.com/jwapps-app/restruo/actions/workflows/docker.yml)
[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

![The Restruo dashboard: three Portainer instances with their stacks, update badges, and down-container alerts](docs/screenshot.png)

## What it does

- **Aggregates every Portainer you own** into one list — stacks *and* containers that
  aren't part of a stack.
- **Update button per stack**: repulls the image(s) and redeploys through the Portainer
  API, preserving env vars. Standalone containers are recreated on a fresh pull.
- **Tells you what actually needs updating.** It compares the digest of the image your
  container is *really running* against the registry, and badges only what's behind.
  Pinned tags (`postgres:16`) are deliberately left alone. **Update all** then touches
  only the flagged items.
- **Red dot for trouble** — containers that are stopped, or running but failing their
  healthcheck.
- **Start / stop** per stack and container, so you can bring something back without
  logging into that machine's Portainer.
- **Cleans up after itself**: prune the leftover images your `:latest` updates leave
  behind, plus unused networks, and — only if you explicitly ask — every unused image
  or unused volumes. (Careful with those two: stopping a stack removes its containers,
  so its images look unused.)
- **Installs as a web app.** Add it to your phone's home screen; a 30-day session means a
  force-closed app reopens signed in.
- Instances are added from a settings page — no config files, no restarts.

Stateless apart from its instance list: Portainer stays the source of truth for stacks.
Built and tested against Portainer 2.x (Community Edition).

## Quick start

**Requirements:** Docker, and at least one Portainer 2.x instance you can reach.

Deploy as a Portainer stack (paste this into **Stacks → Add stack**) or with
`docker compose up -d`:

```yaml
services:
  restruo:
    image: ghcr.io/jwapps-app/restruo:latest
    container_name: restruo
    ports:
      - "8080:8080"
    volumes:
      - restruo-data:/data
    environment:
      - DASHBOARD_PASSWORD=change-me
    restart: unless-stopped

volumes:
  restruo-data:
```

Then open `http://<host>:8080`, sign in as `admin` with that password, and click
**⚙ Instances** to add your Portainers. For each one you can use:

- **Username & password** — the same login you use in the Portainer UI. Restruo exchanges
  it for a session token and re-authenticates when it expires. (Not for OAuth/SSO
  accounts.)
- **API token** — create one under **My account → Access tokens** in that Portainer.
  Preferred: it's revocable without changing your password.

Untick **Verify TLS certificate** for self-signed certs, and use **Test connection**
before saving. Instances persist in the `restruo-data` volume, so they survive updates.

### Configuration

Everything is optional except the password.

| Variable | Default | Purpose |
|----------|---------|---------|
| `DASHBOARD_PASSWORD` | — | **Required.** Dashboard login password |
| `RESTRUO_USERNAME` | `admin` | Dashboard login username |
| `RESTRUO_TITLE` | `Restruo` | Dashboard title |
| `RESTRUO_FLOATING_TAGS` | channel tags | Comma-separated tags treated as rolling. Defaults to the tags that name a channel rather than a version — `latest`, `lts`, `stable`, `release`, `edge`, `main`, `master`, `nightly`, `rolling`, `dev`. Set it to override |
| `RESTRUO_REFRESH_SECONDS` | `180` | How often an open dashboard re-reads container state. `0` disables |
| `RESTRUO_SMTP_USER` / `RESTRUO_SMTP_PASSWORD` | — | Your mail address and password — enough on its own for Gmail, Outlook, Yahoo, iCloud, or Fastmail |
| `RESTRUO_SMTP_HOST` | inferred from the address | Only needed for a provider not in that list, or your own relay |
| `RESTRUO_SMTP_PORT` | `587` | `587` for STARTTLS, `465` for SSL, `25` for a local relay |
| `RESTRUO_EMAIL_TO` | your own address | Where to send notifications (comma-separate several) |
| `RESTRUO_EMAIL_FROM` | `RESTRUO_SMTP_USER` | Sender address |
| `RESTRUO_SMTP_SECURITY` | `starttls` | `starttls`, `ssl`, or `none` |
| `RESTRUO_REGISTRY_AUTH` | — | Logins for private registries, `host=user:token` (comma-separate several) — e.g. `ghcr.io=me:ghp_…` with a `read:packages` token |

For the rest (update-check interval, disabling auth, pre-seeding instances) mount a YAML
file at `/config/config.yaml` — see [`config.example.yaml`](config.example.yaml).

## How updates work

**Applying an update** does exactly what the Portainer UI does:

- **Git-based stack**: `PUT /api/stacks/{id}/git/redeploy` with
  `RepullImageAndRedeploy: true`.
- **Compose/editor stack**: fetches the current stack file, then `PUT /api/stacks/{id}`
  re-sending file and env vars with `PullImage: true`.
- **Standalone container**: Portainer's recreate action with a fresh pull.

Env vars and the stack's `EndpointId` are always re-sent from the live stack object, so a
redeploy never wipes your environment. Swarm stacks use the compose path, which Portainer
performs as a rolling service update.

**Detecting an update** compares digests, downloading nothing:

- Only images on a **floating tag** are checked. A tag that names a *channel* moves under
  you — `latest`, `lts`, `stable`, `release`, `edge`, `main`, `nightly` — and all of those
  are checked by default. A tag containing a digit names a *version* you chose
  (`postgres:16-alpine`, `app:2026.07.2`), reads as **pinned**, and is left alone, which
  is the point of pinning. Override the list with `RESTRUO_FLOATING_TAGS`.
- The comparison uses the digest of the image the container is *actually running*, not
  what the local tag points at — those drift apart when something re-pulls a tag without
  recreating the container.
- Works anonymously against Docker Hub, ghcr.io, lscr.io and other v2 registries. An
  image with no repo digest was never pulled from a registry (built on the box, or made
  by the NAS itself) and is labelled **local** — there is nothing to compare it to. The
  exception is a container whose tag has since been re-pulled onto a newer image: the
  newer one is already on the host and only the container is behind, so that reads as an
  update waiting to be applied.
  Images in a registry that refuses anonymous access are labelled **private**; give
  Restruo a login with `RESTRUO_REGISTRY_AUTH` and they get checked like anything else.
  Neither counts as a failed check.
- Runs every 6 hours (configurable) and whenever you hit **Refresh**, which reloads
  container state immediately and scans registries in the background.

## Email notifications

For a mainstream mail account, two variables are the whole setup:

```
RESTRUO_SMTP_USER=you@gmail.com
RESTRUO_SMTP_PASSWORD=your-app-password
```

The server, port, sender, and recipient are all inferred from that address (Gmail,
Outlook/Hotmail, Yahoo, iCloud and Fastmail are recognised; anything else needs
`RESTRUO_SMTP_HOST`). Restruo then emails you when a check finds something new — grouped by instance, one message per check, and only
for findings it hasn't already reported. Sending is outbound only, so nothing has to be
exposed to the internet and no HTTPS or reverse proxy is involved.

**⚙ Instances → Update notifications** shows the current setting and has a **Send test
email** button, so you can prove the settings work without waiting for the next check.

For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833)
rather than your account password, with `smtp.gmail.com`, port `587`, STARTTLS.

## Updating Portainer and its agents

Restruo refuses to update — or stop — a `portainer/portainer-*` or `portainer/agent`
container, and shows disabled buttons instead.

An agent fails the same way Portainer does, for the same reason: Portainer relays every
command for an agent environment *through that agent*. Recreating it stops the container
carrying the command, so the replacement is never created — the environment goes offline
with the new image pulled and unused, and Portainer can no longer reach that machine to
finish or undo it. Update an agent from its own host:

```sh
docker pull portainer/agent:latest
docker rm -f portainer_agent
docker run -d --name portainer_agent --restart=always \
  -p 9001:9001 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /var/lib/docker/volumes:/var/lib/docker/volumes \
  portainer/agent:latest
```

Check `docker inspect portainer_agent` first and match your own ports and volumes. (Stopping Portainer through its own API is worse than
updating: it kills the connection Restruo would need to start it again. The same guard
covers Restruo's own container.) Portainer dies the moment it stops its own container, so an API-driven
recreate can never finish — it just leaves Portainer stopped with the new image pulled
but unused. (If that happens to you by other means: nothing is damaged, just start the
container again.)

Upgrade it from the host instead, matching your original ports and volumes — check with
`docker inspect portainer` first:

```sh
docker pull portainer/portainer-ce:latest
docker stop portainer && docker rm portainer
docker run -d --name portainer --restart=always \
  -p 9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v portainer_data:/data \
  portainer/portainer-ce:latest
```

Portainer's own config lives in its data volume and survives the recreate.

## Cleaning up

**Clean up** (per instance) prunes, by default, only *dangling* images — the untagged
old versions a re-pull leaves behind, which is where update leftovers accumulate — plus
unused networks. Two options are off by default because they can destroy something you
still want:

- **Every image no container uses.** Stopping a stack in Portainer *removes* its
  containers, so a stopped stack's images count as unused and get deleted. That stack
  then can't start until the images are pulled again — a problem if the machine is
  offline, the tag has moved on, or the image was built locally. Restruo names any
  stopped stacks in the confirmation before you tick this.
- **Unused volumes.** Permanently deletes the data in any volume no container
  references — which, for the same reason, includes the volumes of stopped stacks.

## Security

Restruo holds credentials that can redeploy anything on every machine you connect to it,
so keep auth on and keep it on your LAN. See [SECURITY.md](SECURITY.md) for what's stored
where, deployment expectations, and how to report a vulnerability.

A few behaviours worth knowing:

- **Changing `DASHBOARD_PASSWORD` signs every device out.** Sessions are signed with a
  key derived from the password, so a rotated password takes effect everywhere at once.
- **Ten failed logins from one address blocks that address for fifteen minutes**, for
  both the login form and HTTP basic auth. Failures are logged with the source address.
- **The container runs as an unprivileged user** (`restruo`, uid 1000). On start it makes
  `/data` writable by that user and drops root before the app runs — an existing volume
  from an earlier version needs nothing done to it.
- **Browser sessions must send an `X-Restruo: 1` header on any request that changes
  something** (the page does this itself). It stops a page on another port of the same
  host from reusing the session cookie. Basic-auth callers are unaffected.

## API

Every endpoint except `/healthz`, `/api/login` and the static shell requires auth (session
cookie or HTTP basic, so `curl -u` works).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/instances` | Managed instances + reachability (never secrets) |
| POST | `/api/instances` | Add an instance |
| PUT | `/api/instances/{iid}` | Edit an instance (blank secret = keep existing) |
| DELETE | `/api/instances/{iid}` | Remove an instance |
| POST | `/api/instances/test` | Test a connection without saving |
| GET | `/api/stacks` | All stacks and standalone containers, with state |
| POST | `/api/instances/{iid}/stacks/{sid}/update` | Repull + redeploy one stack |
| POST | `/api/instances/{iid}/stacks/{sid}/start`, `/stop` | Start or stop a stack |
| POST | `/api/instances/{iid}/containers/{cid}/update` | Repull + recreate one container |
| POST | `/api/instances/{iid}/containers/{cid}/start`, `/stop` | Start or stop a container |
| POST | `/api/instances/{iid}/prune` | Remove unused images/networks/volumes |
| GET | `/api/jobs/{id}` | Progress of a redeploy that outlived its request (see below) |
| GET | `/api/updates` | Cached update-check results |
| POST | `/api/check-updates` | Run an update check now |
| POST | `/api/login`, `/api/logout` | Session cookie in / out |
| GET | `/healthz` | Liveness (no auth) |

A stack or container update waits for the deploy and reports what happened. If that
takes longer than 25 seconds the response is `202` with a `jobId`; poll `/api/jobs/{id}`
until `done` is true and read `result`. The page does this for you — it exists so an
update behind a reverse proxy isn't cut off mid-deploy by the proxy's timeout.

Session-cookie requests that change something must also send `X-Restruo: 1`; basic auth
(`curl -u`) does not need it.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
DASHBOARD_PASSWORD=dev .venv/bin/uvicorn app.main:app --reload --port 8080
.venv/bin/pytest
```

The backend is FastAPI (`app/`), the frontend is one dependency-free `web/index.html`,
and the tests mock Portainer and the registries with `httpx.MockTransport` — no live
instance needed.

## Alternatives

Worth knowing about, because they may fit you better:

- **[Watchtower](https://github.com/containrrr/watchtower)** — automatic scheduled
  updates. Hands-off, but no dashboard and it can fight Portainer's view of stacks.
- **[What's-Up-Docker](https://github.com/getwud/wud)** — excellent update *detection* and
  notifications, per-container rather than per-stack.
- **Portainer stack webhooks** — a redeploy URL per stack; fine for one or two, no
  aggregation.
- **Portainer Agent** — consolidates machines into one Portainer as environments. Changes
  your topology and still has no bulk one-click repull.

Restruo exists for the specific gap: several independent Portainers, one page, a manual
button per stack, and a badge that tells you which button is worth pressing.

## License

[AGPL-3.0](LICENSE) — you're free to use, modify, and self-host this software;
if you run a modified version as a network service, you must make your source
available to its users.
