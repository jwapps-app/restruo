<img src="web/icon.svg" alt="restack icon" width="72" align="left" style="margin-right: 12px">

# restack

One dashboard for multiple Portainer instances, with a one-click **repull + redeploy**
button per stack. Replaces the manual per-stack flow (login → stack → editor → update →
re-pull checkbox → deploy) across all your machines.

- Aggregates stacks from any number of Portainer instances (built for four).
- Per-stack **Update** button: repulls image(s) and redeploys via the Portainer REST API.
- **Update all** per instance, search/filter, per-instance reachability badges.
- Instances are managed from a **settings page** in the dashboard — add a Portainer with
  either an **API token** or a **username/password**, test the connection, save.
- Update-available badges for images tracking `:latest` (see below).
- Single small container. Portainer stays the source of truth for stacks; restack only
  persists its instance list (a JSON file on the `/data` volume).

Built and tested against Portainer 2.x (API paths and field casing verified July 2026).

## Setup

### 1. Deploy — no files or folders needed

As a Portainer stack (Repository deploy pointing at this repo, compose path
`docker-compose.yml`) or plain `docker compose up -d --build`. Set one required
environment variable in the stack environment:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DASHBOARD_PASSWORD` | yes | — | Dashboard login password |
| `RESTACK_USERNAME` | no | `admin` | Dashboard login username |
| `RESTACK_TITLE` | no | `restack` | Dashboard title |
| `RESTACK_PORT` | no | `8080` | Host port |

Instance data (the Portainers you add, including their credentials) lives in the
`restack-data` named volume. A YAML config file is entirely optional — mount one at
`/config/config.yaml` only if you want to change update-check intervals, disable auth,
or pre-seed instances (see `config.example.yaml`).

### 2. Add your Portainer instances

Open `http://<host>:8080`, log in, and click **⚙ Instances**. For each Portainer, enter
its URL and pick an auth method:

- **Username & password** — easiest: the same login you use in the Portainer UI. restack
  exchanges it for a session token and re-authenticates automatically when it expires.
  Doesn't work for accounts that sign in via OAuth/SSO.
- **API token** — create one in that Portainer under **My account → Access tokens**.
  Preferred if you want a revocable credential that doesn't expose your password.

Either credential has the full power of its user account — use a least-privileged user if
your edition supports RBAC. Untick **Verify TLS certificate** for self-signed certs.
Use **Test connection** before saving.

Instances persist in `/data/instances.json` (mounted volume), so they survive container
updates. A `config.yaml` `instances:` block is also supported as a one-time seed —
imported on first start, then the settings page takes over.

To run restack *as a Portainer stack* (it can then update itself), paste
`docker-compose.yml` into a new stack and mount `config.yaml` and a data volume.

## How an update works

For each stack, restack does exactly what the UI checkbox flow does:

- **Git-based stack** (has `GitConfig`): `PUT /api/stacks/{id}/git/redeploy` with
  `RepullImageAndRedeploy: true`.
- **Compose/editor stack**: fetches the current stack file, then `PUT /api/stacks/{id}`
  re-sending the file and env vars with `PullImage: true`.

Env vars and the stack's `EndpointId` are always re-sent from the live stack object, so
redeploys never wipe environment variables. Swarm stacks (`Type: 1`) use the compose path;
Portainer performs a rolling service update for those — they're labelled `swarm` in the UI.

## Update notifications

restack can tell you when a newer image is available for a stack:

- Only images that **track `:latest`** (or have no tag, which Docker treats as `latest`)
  are checked. Anything pinned to a version (`mariadb:11`, `img@sha256:…`) is shown as
  **pinned** and deliberately not checked — pin a tag when you *don't* want update noise.
- The check compares the digest of the image on the machine (read via Portainer's Docker
  proxy, using the same API token) against the registry's current digest for the tag —
  nothing is downloaded. Works anonymously with Docker Hub, ghcr.io, lscr.io, and any
  standard v2 registry; locally built images show as not checkable.
- Checks run on a schedule (`updates.interval_hours`, default 6h — keep it modest, Docker
  Hub rate-limits anonymous requests) and on demand via the **Check updates** button.
- Results appear as **⬆ update available** badges per stack and per instance; new findings
  are also written to the container log. The notifier layer is pluggable, so additional
  paths (ntfy, webhooks, …) can be added later.

## Security notes

- **Credentials are powerful** — an API token or password can do anything that Portainer
  user can. They live only in `/data/instances.json` on the server and are never sent
  back to the browser or logged. Protect the `/data` volume accordingly.
- **Keep dashboard auth on.** Without it, anyone who can reach port 8080 can redeploy
  your stacks.
- **LAN only.** Don't expose restack to the internet; if you need remote access, put it
  behind a VPN/Tailscale or an authenticated reverse proxy.
- Rotate tokens periodically. Pass the dashboard password via env var or secret, not in
  the YAML.
- `verify_tls: false` disables certificate checking for that instance — acceptable for a
  home lab with self-signed certs, but prefer real certs where possible.

## API

| Method | Path                                          | Purpose                                  |
|--------|-----------------------------------------------|------------------------------------------|
| GET    | `/api/instances`                              | Managed instances + reachability (no secrets) |
| POST   | `/api/instances`                              | Add an instance                          |
| PUT    | `/api/instances/{iid}`                        | Edit an instance (blank secret = keep)   |
| DELETE | `/api/instances/{iid}`                        | Remove an instance                       |
| POST   | `/api/instances/test`                         | Test a connection without saving         |
| GET    | `/api/stacks`                                 | All stacks across all instances          |
| POST   | `/api/instances/{iid}/stacks/{sid}/update`    | Repull + redeploy one stack              |
| POST   | `/api/update-all`                             | Update every stack (optional `{"instanceId": n}` filter) |
| GET    | `/api/updates`                                | Cached update-check results              |
| POST   | `/api/check-updates`                          | Run an update check now                  |
| GET    | `/healthz`                                    | Container liveness (no auth)             |

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
CONFIG_PATH=config.yaml .venv/bin/uvicorn app.main:app --reload --port 8080
.venv/bin/pytest
```

## Alternatives considered

- **Watchtower** — scheduled auto-updates of running containers; hands-off but no
  per-stack dashboard/button, and can fight Portainer's view of stacks.
- **What's-Up-Docker** — detects available image updates and notifies; a detector more
  than a redeployer.
- **Portainer stack webhooks** — per-stack redeploy URLs; simple for one stack but no
  aggregation UI and limited repull behavior.
- **Portainer Agent consolidation** — add the other machines as environments in one
  Portainer; changes your topology and still no bulk one-click repull.

None provide a multi-instance dashboard with a manual repull+redeploy button — hence restack.
