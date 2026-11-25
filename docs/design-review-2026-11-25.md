# Design Review: Restruo (2026-11-25)

Reviewed at commit `d6ccc7b`. Read-only: no code was changed. This review is about
design complexity (what makes the next change expensive), not about bugs. All 287
tests pass in 8.9 s.

## Status: all eight priorities applied (2026-11-25)

The sections below are the review as written, against `d6ccc7b`. This section
records what was then done about it. The work is in the working tree and is not
committed. Tests: 332 pass (287 before, plus 45 for the new modules). The page was
exercised in a browser against a simulated Portainer; nothing was run against a
real one.

| # | Item | Status | What changed |
|---|---|---|---|
| 1 | Operations engine in the routing module | Done | `main.py` is 567 lines (was 1,478) and holds routes and wiring. New: `actions.py`, `operations.py`, `outcome.py`, `helper_runner.py`. Auth dependencies moved into `auth.py`. Refusals are `errors.Refused` subclasses, turned into responses by one handler. |
| 2 | Two walks of the same Portainer | Done | `inventory.read_inventory()` is the only walk. `dashboard.py` formats it; `UpdateChecker._check_instance` consumes it. `_container_ref` and its memo are gone. Step 4 (server-side join of update info into `/api/stacks`) was marked optional and is not done. |
| 3 | Page re-derives server rules | Done | Rows carry `helper`, `selfCritical`, `isSelf`, `stopped`, `running`. The page has no image-name or state-word matching left. `STATUS` is defined once in the script. |
| 4 | Operation state in the DOM | Done | One `api()` helper; an `ops` map that `render()` draws from; `clearUpdateFlags` and `busyCount` are gone; Update all is built from data. |
| 5 | Results and error text by hand | Done | `errors.describe()` is the only formatter; `operations.Timed` builds every result; start, stop and update answer through one response helper. |
| 6 | Settings in six files | Done | `tests/test_settings.py` fails when a `RESTRUO_*` name is missing from the compose file, `.env.example` or the README, and when anything but `config.py` reads the environment. The three gaps it found are filled. `UpdateChecker` takes `config.updates`. |
| 7 | Tests filed by date, many fakes | Steps 1 and 2 | `tests/conftest.py` holds the shared fixtures; `tests/fakes.py` has `FakePortainer`, used by the new tests. Existing fakes and file names are unchanged: step 3 was "when a file is next edited". No test imports a private name from `app.main` any more. |
| 8 | Health and timeouts defined several times | Done | `health.judge()` is used by the container watcher and the helper; the list-based checks sit beside it. `timing.py` holds every deadline, derived ones computed; `tests/test_timing.py` asserts the order; the page is sent its job-wait window. |

### Behaviour that changed

Unifying two versions of a rule means picking one. These are the places where
what a user or a script sees is different from before.

- **Error wording.** A failure that is not Portainer's own answer now always names
  its type (`ConnectError: All connection attempts failed`). In update-check
  details a Portainer failure now reads as Portainer's message instead of
  `PortainerError: Portainer returned 404: …`.
- **Result body.** Update, start and stop answer `{"ok", "name", "durationMs",
  "message"}`. The key was `stack`, also for containers. The page never read it.
- **Stop is disabled on a standalone Restruo container.** The server already
  refused it; the page now takes the server's flag instead of its own narrower rule.
- **Stack flags see through a moved tag.** `selfCritical` and `updateProtected` on
  a stack row are computed from what its containers were created from, as the
  server-side guards already were.
- **Inventory edge cases.** With a stack file that cannot be read, the dashboard
  shows what the containers run (it showed nothing; the checker already did this).
  When the environments cannot be listed, the environments stacks point at are
  still read.
- **A row's two buttons are both disabled while either is working.** The server
  refused the second click with a 409 before.
- **Sign out returns to the login screen.** The handler threw on a missing
  element (`#stacks`) before reaching `showLogin`.
- **A 422 from the instance form is shown as its messages**, not `[object Object]`.
- **The stack Portainer returns for an action is checked** for a usable id and
  environment before anything is done with it (optional item, folded in).
- **A failed stack fetch before start or stop is a 502 with a reason.** A
  non-Portainer failure there was an unhandled 500.
- **The target is claimed before the job starts**, closing a window in which two
  requests could both pass the conflict check.
- **Loggers** are named for their module: `restruo.auth`, `restruo.dashboard`,
  `restruo.helper`, `restruo.registry`, `restruo.notifiers`.
- **Settings.** `log_level`, `helper_image` and `data_path` can also be set in the
  YAML file. A mistyped `RESTRUO_LOG_LEVEL` is refused at start with a reason.

### Left as it was

- **The helper's pull can outlast the 15 minutes it is watched for.** Fixing it
  means changing a timeout. It is written down in `timing.py` next to
  `HELPER_TIMEOUT_SECONDS`.
- **The helper container is not removed after a timeout or a failed start**
  (`helper_runner.replace_with_helper`). It was noted below as a bug outside
  this review and is still open.
- **The optional list**, except what fell out of the work above: the dead
  docstring, the misplaced comment, the duplicate socket constant, the logger
  names, the overlapping comments in `config.example.yaml`, and the unvalidated
  `get_stack` answer.

## Architecture summary

- **One process, two halves.** A FastAPI backend (`app/`, 4,294 lines) and one
  dependency-free page (`web/index.html`, 1,320 lines, about 1,040 of them script).
  Portainer is the source of truth; Restruo keeps only an instance list, a session
  secret and a "what was already emailed" file under `/data`.
- **`app/main.py` (1,478 lines)** is the routing module and also the operations
  engine: auth dependencies, instance CRUD, the dashboard aggregation, the
  one-at-a-time lock, the job runner, the deploy and container watchers, helper
  orchestration, stopped-stack updates and prune.
- **`app/portainer.py` (927)** is the Portainer client (session, CSRF and reconnect
  healing, stack calls, Docker-proxy calls) plus module-level domain functions:
  compose parsing, variable interpolation, network safety, image classification and
  the dashboard row shapes.
- **`app/updates.py` (564)** is `UpdateChecker`: walks every instance, compares
  running digests against the registry (`app/registry.py`, 194), caches results in
  memory and hands new findings to `app/notifiers.py` (116).
- **`app/helper.py` (379)** is a separate program run as a short-lived container on
  the target host to replace Portainer or an agent. Synchronous, socket-only.
- **`app/instances.py` (276)** is the JSON store and `ClientManager`;
  **`app/config.py` (231)** is pydantic config from env plus optional YAML;
  **`app/auth.py` (109)** is session signing and the login limiter.
- **Data flow.** The page calls `GET /api/stacks` (live state) and
  `GET /api/updates` (cached check) separately and joins the two in the browser by
  instance, stack id, and container id plus environment. Actions POST per row; long
  ones return `202` with a job id that the page polls.
- **Hotspots** (commits in 6 months, of 73): `web/index.html` 45, `app/main.py` 40,
  `app/portainer.py` 30, `README.md` 28, `app/updates.py` 22. `main.py` has grown
  from 365 to 1,478 lines since July; 31 commits touched `main.py` and `index.html`
  together.

## Overall assessment

The individual pieces are careful: failure states are named rather than swallowed,
comments explain why, and the test suite is fast and mock-only. The drift is at
the seams. Features have been added to the two files that were already open
(`main.py` and `index.html`), so each now holds several jobs, and the same
knowledge has been written down more than once: twice in Python (two walks of a
Portainer) and again in JavaScript (which containers need the helper, what counts
as stopped). The last two feature commits touched 10 and 7 files. The biggest
theme is that there is no layer between "HTTP route" and "Portainer call" where an
operation or an inventory lives, so both the routes and the browser carry rules
that belong in one place.

### Change amplification, traced

| A plausible next change | Edit points today |
|---|---|
| Add a setting (as `RESTRUO_CHECK_VERSION_TAGS` was, in `7d601f6`) | `config.py`, `main.py` lifespan wiring, `docker-compose.yml` pass-through list, `.env.example`, `config.example.yaml`, README table: 6 files |
| Add a field to an instance (a group, a "skip in Update all" flag) | `InstanceRecord` + `public()`, `InstanceInput` + `to_fields()`, `InstanceConfig` + the seed mapping in `lifespan`, and in the page `formBody()`, `fillForm()`, the form markup and the list row: 8 edit points in 4 files |
| Treat another image as "needs the helper" or "must not be stopped" | `portainer.py:863-889`, `main.py:1302`, `index.html:330-342` and `index.html:1000`: 4 places, 2 languages |
| Add a new update status | `updates.py` constants and `_check_image`, `mark_updated`, `_notify_new`; in the page `imgLine`, `unknownBadge`, three counters and `clearUpdateFlags`; README |
| Add an action (restart, pull only) | 2 routes + a `_set_*_state` twin in `main.py`, a `_conflicts` key, a client method, and in the page `powerBtn`, `runPower` and the click dispatcher |

## Top priorities

### 1. The operations engine lives inside the routing module

- **Where:** `app/main.py:592-1331`. `_conflicts` / `_exclusive` (592-642),
  `_await_deploy` (686-726), `_run_job` (743-775), helper orchestration (778-876),
  `_update_stopped_stack` (896-966), `_update_one` (969-996), `_await_container`
  (1191-1246), `_stop_again` (1249-1264). Auth dependencies sit at 164-249 although
  `app/auth.py` exists.
- **Symptom:** god file; missing seam for testing.
- **Why it matters:** this is where features land (40 commits, 4x growth). The
  engine reaches its state through `request.app.state` (`in_flight`, `jobs`,
  `checker`), so nothing can run an operation without a FastAPI request. Tests
  already work around that: `tests/test_concurrent_updates.py:47-53` builds a
  `FakeRequest` and `FakeApp`, and five test files import underscore-private names
  from `main` (`_await_container` 9 times, `_await_deploy`, `_fingerprint`,
  `_conflicts`, `_exclusive`, `_find_container`) and monkeypatch its polling
  constants. Anything that is not a click, such as a scheduled auto-update or a
  future "update all" endpoint, cannot reuse the lock or the job runner as written.
- **Fix:** incremental, each step a pure move with tests green after it.
  1. Move the watchers that take no `request` (`_fingerprint`, `_container_working`,
     `_await_deploy`, `_await_container`, `_last_log_line`, `_stop_again`) to
     `app/outcome.py`. Repoint the tests' imports.
  2. Move `helper_image`, `helper_spec`, `parse_helper_result`, `_remove_helper`,
     `_replace_with_helper` to `app/helper_runner.py`.
  3. Introduce one `Operations` object, created in `lifespan`, that owns
     `in_flight` and `jobs` and exposes `exclusive(key)` and `run(key, work)`.
     Drop the duplicate conflict pre-check at `main.py:746-749`.
  4. Move `require_auth` and its helpers into `app/auth.py`.
  Routes are then: parse, fetch the stack, call an operation, shape the response.
- **Pain / Frequency / Effort:** High / High / M

### 2. Two independent walks of the same Portainer, joined in the browser

- **Where:** `_stacks_for_instance` (`app/main.py:462-572`) and
  `UpdateChecker._check_instance` (`app/updates.py:305-437`). The join is
  `updateInfoFor` / `updateInfoForCtr` (`web/index.html:308-325`).
- **Symptom:** duplicated logic; information leakage.
- **Why it matters:** both functions list stacks, endpoints and containers, match
  containers to stacks, fetch stack files behind a semaphore of 6
  (`DASHBOARD_CONCURRENCY` in one, a literal `6` in the other), resolve images and
  pick out standalone containers. Every rule about "what runs where" has to be
  taught to both: 17 of the 22 commits to `updates.py` also touched `main.py`. They
  already disagree in small ways: environment errors are keyed by name in one and
  by id in the other; a bare `sha256:` image is resolved by `resolve_image_name`
  (`portainer.py:795`) in one and by `_container_ref` (`updates.py:112`) in the
  other, with different fallbacks; a failed stack listing is reported through
  `describe()` in one and `str(exc)` in the other (`updates.py:315`), so the same
  failure reads differently. Because the join happens in the page, the
  cloned-hosts fix had to be made three times (`updates.py:457-461`,
  `updates.py:523-528`, `index.html:313-325`), and the page needs a comment
  (`index.html:544-547`) explaining why it cannot trust the cached counts.
- **Fix:**
  1. Add `app/inventory.py` with `async def read_inventory(client) -> Inventory`:
     stacks with their own containers, stack file and resolved images; standalone
     containers; environment names; per-environment errors. Characterise it with
     the existing fakes first.
  2. Have `_stacks_for_instance` format an `Inventory` (behaviour unchanged).
  3. Have `_check_instance` consume the same `Inventory`; delete `_container_ref`.
  4. Later, and optional: attach the cached update info to each row in
     `/api/stacks`, so the page stops joining two responses.
- **Pain / Frequency / Effort:** High / High / M

### 3. The page re-derives rules the server already knows

- **Where:** `cannotRecreate` and `helperKind` (`web/index.html:330-342`) copy
  `cannot_recreate_image` (`app/portainer.py:863-877`) and the kind decision at
  `app/main.py:1302`. The "is this Restruo" test is `index.html:1000` and
  `portainer.py:889`. The stopped-state list `["exited","created","dead"]` is
  `index.html:525` and `main.py:1307`. `clearUpdateFlags` (`index.html:709-751`)
  mirrors `UpdateChecker.mark_updated` (`updates.py:439-464`).
- **Symptom:** information leakage across the language boundary.
- **Why it matters:** stack rows get `selfCritical` and `updateProtected` from the
  server (`main.py:549-554`); container rows do not, so the page classifies them
  itself by substring. The consequence is larger than a duplicated string: the
  rule "Portainer itself is never part of Update all" exists only as
  `b.dataset.helper !== "portainer"` at `index.html:1036`, and "agents go last" only
  at `index.html:1064-1065`. Change the server's notion of what needs the helper
  and the confirmation text, the disabled Stop button and the Update-all exclusion
  follow only if someone remembers the JavaScript copy.
- **Fix:**
  1. In `normalize_container`, emit `helper: "agent" | "portainer" | ""`,
     `selfCritical`, `isSelf` and `stopped`; add `isSelf` and `stopped` to stack
     rows. Classify from the resolved image, in one function.
  2. Replace `cannotRecreate`, `helperKind`, the `restruo` substring test and the
     state list in the page with those fields.
  3. Define the status strings once (a `Status` enum in `updates.py`, a matching
     `const STATUS` at the top of the script) instead of 28 literals.
- **Pain / Frequency / Effort:** High / High / S

### 4. In the page, operation state lives in the DOM

- **Where:** `web/index.html`: `render()` (426-604), `clearUpdateFlags` (709-751),
  `runUpdate` (780-830), `runPower` (932-961), Update all (1021-1068).
- **Symptom:** tangled responsibilities; duplicated logic.
- **Why it matters:** this is the most-changed file (45 commits). `render()`
  rebuilds everything with `innerHTML`, which would wipe a spinner or a result
  message, so the code works around its own renderer: `busyCount` pauses
  auto-refresh while anything runs, and `clearUpdateFlags` spends 40 lines editing
  badges, titles and header counts by hand instead of re-rendering. Update all
  decides what to update by querying CSS (`.update-btn.has-upd`,
  `index.html:1035`). The row key is computed three times (344, 782, 934). The
  `401` to login handling appears at 6 of about 13 fetch sites and the
  `body.detail || HTTP ${status}` extraction 6 times.
- **Fix:** keeps the single dependency-free file.
  1. Add one `api(path, opts)` helper: sends the header, parses JSON, turns `401`
     into `showLogin(true)`, throws a uniform error. Replace call sites one by one.
  2. Add `const ops = new Map()` keyed by row key, holding `{phase, message}`.
     `stackRow` and `ctrRow` read button label, disabled state and message from it.
  3. `runUpdate` and `runPower` then write to `ops` and call `render()`; delete
     `clearUpdateFlags`' DOM surgery and build the Update-all list from data.
- **Pain / Frequency / Effort:** Med / High / M

### 5. Operation results and error text are assembled by hand at every site

- **Where:** the `{"ok", "stack", "durationMs", "message"}` dict is built 8 times
  (`main.py:912, 977, 984, 991, 1019, 1294, 1313, 1327`); `_timed` (1018) exists
  and is used by 2 of them. Error text comes from `describe` (`main.py:879`),
  `describe_error` (`updates.py:40`), an inline copy (`portainer.py:570`),
  `f"{type(exc).__name__}: {exc}"` (`main.py:1427`), `str(exc)` (`updates.py:315`)
  and `f"...{exc}"` (`main.py:1149`). Four `except PortainerError` /
  `except Exception` pairs have identical bodies (`main.py:337-340, 352-361,
  472-485, 976-989`) although `describe` already handles both.
- **Symptom:** duplicated logic; scattered error handling; naming.
- **Why it matters:** `main.py` has 31 `except Exception` blocks, each choosing its
  own wording, so one Portainer failure shows as "Unable to update stack: …" in
  one place and "Portainer returned 500: …" in another. The response contract
  differs per action: update answers 200/502/202, start and stop 200 or a 502 with
  a body, prune and test a 200 with `ok: false`; the page copes with
  `body.message || body.detail || HTTP …`. The key is called `stack` even when it
  holds a container name. `_msg` (`main.py:1358`) wraps `describe` and is passed
  into `_prune_endpoints` as a parameter for no benefit.
- **Fix:**
  1. One `describe(exc)` in a small `app/errors.py`; `updates.py` and
     `portainer.py` import it. Collapse the paired `except` blocks.
  2. An `OpResult` dataclass with `.ok(name, message)`, `.fail(name, message)` and
     a `started` clock, serialised in one place. Emit `name` alongside `stack`
     until the page is switched, then drop `stack`.
  3. Have start, stop and update all return through the same helper so the page
     reads one shape.
- **Pain / Frequency / Effort:** Med / High / S

### 6. A setting is declared in six files, and some are read outside the config

- **Where:** `app/config.py` (fields with env `default_factory`), `lifespan`
  wiring (`main.py:104-116`), `docker-compose.yml:17-32`, `.env.example`,
  `config.example.yaml`, README table. Environment is also read directly at
  `main.py:59` (`RESTRUO_LOG_LEVEL`), `main.py:787-790` and `main.py:1439`
  (`RESTRUO_HELPER_IMAGE`, `RESTRUO_VERSION`) and `instances.py:97` (`DATA_PATH`).
- **Symptom:** change amplification; information leakage.
- **Why it matters:** the compose file passes through only the variables it lists,
  so a setting missing from that list silently does nothing for anyone deploying
  with it. That has already happened: `RESTRUO_LOG_LEVEL` is read by the app and
  appears in none of the compose file, `.env.example` or the README;
  `RESTRUO_EMAIL_FROM` and `RESTRUO_HELPER_IMAGE` are missing from `.env.example`.
  `UpdateChecker` takes four config values as separate keyword arguments, so each
  new update option is also a signature change and a wiring line.
- **Fix:**
  1. Add a test that collects every `RESTRUO_*` name in `app/` and asserts each
     appears in `docker-compose.yml`, `.env.example` and the README (with a short
     allow-list for build-time names such as `RESTRUO_VERSION`). This makes the
     leak fail loudly; it is the cheapest step and worth doing alone.
  2. Move `helper_image`, version, log level and data path onto `AppConfig`.
  3. Pass `config.updates` to `UpdateChecker` instead of unpacking it.
- **Pain / Frequency / Effort:** Med / Med / S

### 7. Tests are filed by when they were written, each with its own fake Portainer

- **Where:** `tests/test_audit_fixes.py` (436 lines), `tests/test_audit_round2.py`
  (615), `tests/test_issues_18_19.py` (317). No `tests/conftest.py`.
- **Symptom:** duplicated logic; unclear naming.
- **Why it matters:** the `client` fixture and its `_reset` helper are copied into
  4 files. There are 42 `httpx.MockTransport` constructions and about 14 distinct
  hand-written fakes of the same Portainer API (`StackHost`, `AgentHost`,
  `StoppedStackHost`, `ContainerHost`, `Host`, `FakeClient`, `portainer_transport`,
  `container_aware_transport`, …). A change to how Restruo calls Portainer, such
  as item 2, means updating many fakes that each model a slightly different
  subset. To find what covers `_await_deploy` you look in
  `test_deploy_completion.py` and in both audit files. Five assertions check
  JavaScript by matching source text in `index.html`, which will break on any
  rewording under item 4.
- **Fix:**
  1. Add `tests/conftest.py` with the shared `client` fixture; delete the copies.
  2. Add one `FakePortainer` (stacks, endpoints, containers, images as plain
     attributes; records requests) and use it for new tests. Convert old fakes
     only when a test is being touched anyway.
  3. When a file is next edited, move its tests to a file named for the module
     they cover.
- **Pain / Frequency / Effort:** Med / Med / S (step 1), M (step 2)

### 8. "Is this container working" and "how long do we wait" are each defined several times

- **Where:** `container_is_down` (`portainer.py:892`), `_container_working`
  (`main.py:673`), the loop in `_await_container` (`main.py:1214-1239`) and
  `wait_ready` (`helper.py:257`). Timing: `REDEPLOY_TIMEOUT` (`portainer.py:27`),
  `DEPLOY_TIMEOUT_SECONDS` (`main.py:651`), `HELPER_TIMEOUT_SECONDS`
  (`main.py:780`), `RETIRE_AFTER_SECONDS` (`instances.py:26`), the helper's read
  timeout (`helper.py:53`), `JOB_RETENTION_SECONDS` (`main.py:734`) and the page's
  forty-minute wait (`index.html:837`).
- **Symptom:** information leakage; duplicated logic.
- **Why it matters:** the four health rules differ. A container that exits 0 is a
  success in two of them, a failure in `wait_ready` and "down" in
  `container_is_down`. The timeouts depend on each other, and the dependencies are
  recorded only in comments ("longer than any one job can run", "a helper may run
  for fifteen minutes"). One has already drifted: the helper's pull has no overall
  bound (`helper.py:53`) while the dashboard stops watching after 15 minutes
  (`main.py:780`) and retires clients after 20.
- **Fix:**
  1. One pure function `judge(inspect_state) -> "working" | "starting" | "failed"`
     in a module both `app/helper.py` and the watcher can import (the helper runs
     from the same image). Keep the list-based check separate but next to it.
  2. Put the timeouts in one `app/timing.py`, with the derived ones computed
     (`RETIRE_AFTER = HELPER_TIMEOUT + margin`) and one test asserting the order.
- **Pain / Frequency / Effort:** Med / Low / S

## Optional / later

- **Three models of an instance** (`InstanceConfig` in `config.py:29`,
  `InstanceRecord` in `instances.py:38`, `InstanceInput` in `main.py:308`) with
  hand-written mappings (`to_fields`, `public`, the seed dict at `main.py:82-88`).
  A pydantic alias generator would remove `to_fields` and `public`. Worth doing
  the next time an instance gains a field.
- **`portainer.py` mixes client and domain code.** Compose parsing
  (`extract_images`, `interpolate`, `stack_images`, `profiled_images`) is a pure
  move to `app/compose.py`; `normalize_stack` / `normalize_container` belong with
  the inventory from item 2.
- **Three near-identical "repo:tag" normalisers**: `updates.py:158`,
  `helper.py:125`, `registry.py:62-72`; and the registry-host rule is restated in
  `registry_id_for` (`portainer.py:187-193`, which says so in its comment).
- **Validation is attached to list calls, not to the data.** `list_stacks` filters
  through `well_formed`; `get_stack` (`portainer.py:478`) does not, and routes then
  index `stack["EndpointId"]` (`main.py:1070, 1170`).
- **Lock keys are tuples of varying length** read by position (`key[1]`, `key[2]`
  in `_conflicts`). A small frozen dataclass would name the fields.
- **Raw Portainer dicts flow end to end**, mixing `stack["EndpointId"]` and
  `stack.get("EndpointId")`. Acceptable at this size; revisit with item 2.
- **Vestigial bits:** a second, dead docstring in `update_container`
  (`main.py:1274`); the comment at `main.py:1407` sits above `test_email` but
  describes `ui_config`; `redeploy_compose` and `redeploy_git` take a `prune`
  argument nothing passes; `HELPER_SOCKET` (`main.py:778`) duplicates
  `helper.DOCKER_SOCKET`; validators named `strip_trailing_slash` that validate the
  URL; `registry.py` and `notifiers.py` log as `restruo.updates`;
  `getattr(instance, "auth_type", "api_key")` (`portainer.py:272`) guards against a
  type that no longer occurs; `config.example.yaml:24-27` has two overlapping
  comment blocks.
- **`restruo-code-audit.md`** (53 KB, absolute local paths) is tracked at the repo
  root; consider moving it under `docs/` or out of the repo.

Noticed in passing, outside the scope of a design review: `_replace_with_helper`
returns without removing the helper container when it times out
(`main.py:874-876`) or when create succeeds and start fails (`main.py:848-853`).

## What's already good

- **Pure decision functions.** `_conflicts`, `networks_safe_to_remove`,
  `realm_policy`, `interpolate`, `inherited_config`, `mounts_to_carry` and
  `parse_helper_result` take data and return a decision, and are tested as such.
  Items 1, 2 and 8 are mostly about giving more code this shape.
- **Transport injection.** `PortainerClient`, `RegistryClient` and `helper.Docker`
  all accept a transport, which is why 287 tests run in under nine seconds with
  no live system.
- **`PortainerClient._request` is a deep module.** Callers write one line; login,
  JWT refresh, CSRF harvesting, origin headers and stale-pool recovery are hidden
  behind it.
- **"Could not look" is never reported as "nothing there".** Listing failures
  carry through as `unchecked` / `environmentErrors` rather than as empty lists.
- **`InstanceStore._save`** persists before it publishes, so memory, disk and the
  running clients cannot disagree.
- **The `Notifier` interface** is a real seam; a second notification path would be
  one class and one line in `build_notifiers`.
- **Comments say why.** Almost every non-obvious line records the failure that
  motivated it.

## Suggested design principles

There is no project instructions file in this repo today. These are drafted for
one; nothing has been added.

- Route handlers in `app/main.py` parse the request, call one function and shape
  the response. Anything that polls, waits or decides goes in a module that does
  not import `fastapi`.
- Only code under the `Operations` object touches `in_flight` or `jobs`; nothing
  outside `main.py` and the HTTP half of `auth.py` takes a `Request`.
- One walk of a Portainer: anything that needs stacks, containers or images reads
  an `Inventory`. Do not add another `list_stacks` / `list_containers` loop.
- The server classifies, the page renders. A rule about an image or a state
  (needs the helper, must not be stopped, is stopped, is Restruo) is computed in
  Python and sent as a field on the row; `web/index.html` never matches on image
  names or state strings.
- Every operation returns an `OpResult`, and every error message comes from
  `describe()`. No hand-built result dicts, no `str(exc)` in a response.
- Every setting is a field on `AppConfig`. A new `RESTRUO_*` variable is added to
  `docker-compose.yml`, `.env.example` and the README in the same change; the
  settings test enforces it.
- In the page, state lives in variables (`data`, `updates`, `ops`) and `render()`
  draws from them. Do not patch the DOM after the fact or read state back out of
  classes.
- New tests go in a file named for the module under test and use the shared
  `client` fixture and `FakePortainer`. Do not import underscore-private names
  from `app.main`.
- Timeouts live in one module; a timeout that must exceed another is computed
  from it.
- A feature that will touch more than three files gets a short plan first, naming
  which module owns each new rule.
