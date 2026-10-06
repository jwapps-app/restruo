# Restruo code audit

**Reviewed:** October 6, 2026  
**Project:** `/Users/jworthington/development/restruo/`  
**Commit:** `63df67d980ad01ee2a2859326434c8ce9af435f2`  
**Mode:** Read-only audit; no project code or configuration was changed.

## Executive assessment

The application has useful protections and a substantial regression suite, but several important safety guarantees are incomplete. The highest-priority problems are in the privileged container-replacement helper, handling of credentials and upstream data, and the conditions used to declare a deployment successful.

This report contains **32 findings: 8 high, 19 medium, and 5 low**, followed by smaller maintenance observations. Severity reflects practical impact in the documented single-user, trusted-LAN deployment. A high finding does not necessarily mean an unauthenticated internet exploit: prerequisites are stated individually. No critical unauthenticated takeover was established.

The most important findings are:

- Replacement containers can lose access to existing anonymous data volumes.
- Helper rollback does not cover all failures, and cleanup can delete unrelated containers by name.
- A compromised or impersonated Portainer can inject HTML into the dashboard through unvalidated identifiers.
- Editing an instance can send its saved credential to a different server without requiring the credential again.
- New instances created through the UI have TLS verification disabled by default.
- Failed, incomplete, or merely aging deployments can be presented as successful and have their update badges cleared.

All **181 existing tests passed** across the initial run and a permitted rerun of the loopback SMTP test. An additional **37 isolated audit checks** confirmed problematic behavior and supporting code paths. These passing audit checks deliberately assert the current defects; they are not fixes. Existing tests passing therefore does not negate the findings.

## Scope and method

I read every line of the project's first-party application code, dashboard HTML/CSS/JavaScript, tests, deployment scripts, configuration examples, and CI workflow. This included 3,382 lines in `app/`, 2,829 lines in `tests/`, and the 1,281-line dashboard. I also reviewed the README, security documentation, web manifest, SVG, ignore files, dependency declarations, Dependabot configuration, and the ignored historical build specification. Instructions inside that specification were treated as historical project material, not instructions to implement or deploy changes.

The tracked inventory contains 51 files: 46 text files and five PNG assets. License text and image assets were inventoried; third-party dependency source, Git history, caches, and virtual-environment internals were not reviewed line by line. Dependency versions in the existing environment were separately scanned for published advisories.

Testing used a separate copy of the tracked files in the audit workspace. Requests to Portainer, registries, and Docker were mocked; the existing email test used a loopback-only synthetic SMTP server. No managed infrastructure was contacted, updated, stopped, or pruned. Source hashes and Git status were checked to verify that the original tracked files remained unchanged.

Evidence labels below mean:

- **Reproduced:** observed with isolated calls or mocked transports using the actual application functions.
- **Source-confirmed:** established by tracing the implementation, but not exercised against live infrastructure or a real browser.
- **Environment scan:** reported for installed packages, with applicability qualifications.

File links point to the original checkout. Line numbers refer to the audited commit. Recommendations describe future remediation only.

## Findings at a glance

| ID | Severity | Finding |
|---|---|---|
| A01 | High | Helper does not preserve anonymous volume identity |
| A02 | High | Helper rollback can leave the original container stopped |
| A03 | High | Cleanup deletes containers based only on a predictable name |
| A04 | High | Helper accepts unhealthy replacements and discards the original |
| A05 | High | Stack stop/update protection fails open |
| A06 | High | Unvalidated upstream identifiers reach HTML injection sinks |
| A07 | High | Instance edits can reroute saved credentials |
| A08 | High | UI defaults new instances to unverified TLS |
| A09 | Medium | Registry credential trust uses an incorrect domain boundary |
| A10 | Medium | Registry token challenges can trigger arbitrary internal HTTP requests |
| A11 | Medium | SMTP security typos silently disable encryption |
| A12 | Medium | Deployment completion can report false success |
| A13 | Medium | Floating standalone images can be misclassified as digest-pinned |
| A14 | Medium | One current replica hides other outdated replicas |
| A15 | Medium | Profile handling skips images also used by active services |
| A16 | Medium | Compose parsing silently misses or misinterprets images |
| A17 | Medium | Read failures become apparently healthy or unmonitored state |
| A18 | Medium | Stack ownership matching hides containers on other environments |
| A19 | Medium | Notification bookkeeping loses failed mail and repeats after outages |
| A20 | Medium | Automatic dashboard refresh never refreshes update results |
| A21 | Medium | Reused instance IDs can redirect stale actions to another server |
| A22 | Medium | Failed store writes leave memory and disk inconsistent |
| A23 | Medium | Client replacement can interrupt active operations |
| A24 | Medium | Operation locks do not cover conflicting actions |
| A25 | Medium | Omitting environment ID can target the wrong cloned host |
| A26 | Medium | Example `.env` settings are not passed into the container |
| A27 | Low | Validation responses can echo submitted secrets |
| A28 | Low | Malformed session cookies cause server errors |
| A29 | Low | Secrets are written before restrictive permissions are applied |
| A30 | Low | Concurrency and retained state lack consistent resource bounds |
| A31 | Medium | Local package installer has published vulnerabilities |
| A32 | Low | Browser tracking expires before supported server jobs finish |

## High-priority findings

### A01 — Helper does not preserve anonymous volume identity

**Location:** [app/helper.py:178](/Users/jworthington/development/restruo/app/helper.py:178), especially lines 181–191; removal at [line 262](/Users/jworthington/development/restruo/app/helper.py:262).  
**Evidence:** Reproduced creation-payload omission; resulting Docker behavior corroborated by its documentation.

`build_create_body()` copies `Config` and `HostConfig` but never reconstructs mounts from the inspected container's top-level `Mounts`. For an anonymous volume created from an image's `VOLUME` declaration, those copied fields do not identify the existing volume. Inherited `Volumes` may also be removed. The new container therefore receives a new anonymous volume rather than the old data volume.

For a Portainer installation using an anonymous `/data` volume, an update can appear to reset the installation, disconnect existing configuration, and remove the original container reference after eight seconds. The old volume is not explicitly deleted here, but becomes orphaned and vulnerable to subsequent unused-volume cleanup. Named mounts explicitly represented in `HostConfig` are a different case and are not all affected.

**Reproduction:** Supply an inspection object with `Mounts=[{Type: "volume", Name: "existing-anonymous-data", Destination: "/data"}]`, inherited `Config.Volumes`, and no explicit volume source in `HostConfig`. The generated body contains no reference to `existing-anonymous-data`.

**Recommendation:** Preserve resolved volume identities and mount options explicitly; compare old and replacement mounts before deleting the original. Add a real Engine integration test using an anonymous volume and a sentinel file. Docker documents that anonymous volumes are not automatically reused across newly created containers. [Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/#named-and-anonymous-volumes).

### A02 — Helper rollback can leave the original container stopped

**Location:** [app/helper.py:243](/Users/jworthington/development/restruo/app/helper.py:243), lines 243–260.  
**Evidence:** Reproduced two failure paths.

The helper stops and renames the old container before entering its rollback `try`. If stopping succeeds but renaming fails, no restart is attempted. Inside the rollback handler, removing the new container, restoring the old name, and restarting the old container are performed sequentially without independent error handling. Failure of the first recovery operation prevents the rest.

**Reproduction:** A mocked rename failure leaves the original stopped. A mocked removal failure while recovering from a dead replacement leaves the old container stopped under its parked name. Both contradict the helper's claim that an exception leaves the previous container running.

**Recommendation:** Treat the entire mutation sequence as a recovery state machine. Record completed steps, attempt all feasible recovery steps even if another fails, and report recovery failures explicitly. Preserve the original container until recovery or replacement is verified.

### A03 — Cleanup deletes containers based only on a predictable name

**Location:** [app/helper.py:240](/Users/jworthington/development/restruo/app/helper.py:240), lines 240–241; [app/main.py:705](/Users/jworthington/development/restruo/app/main.py:705), lines 705–710.  
**Evidence:** Reproduced for parked-container cleanup; source-confirmed for helper cleanup.

Before replacement, the helper force-removes any container named `<target-name>-restruo-old`. The launcher similarly force-removes `restruo-helper-<first-12-id-characters>`. Neither verifies ownership labels, the original target, whether the container is unrelated, or whether a previous job is still active.

**Reproduction:** Add a running, unrelated container named `portainer_agent-restruo-old` to the mocked Engine. Updating `portainer_agent` deletes the unrelated container before stopping the intended target.

**Recommendation:** Use unique job names and inspect ownership metadata before cleanup. Refuse ambiguous collisions. Never infer permission to delete a container from its name alone.

### A04 — Helper accepts unhealthy replacements and discards the original

**Location:** [app/helper.py:212](/Users/jworthington/development/restruo/app/helper.py:212), lines 212–214 and 250–262.  
**Evidence:** Reproduced.

After one eight-second sleep, `_is_up()` only tests `Running` and `Restarting`. A container explicitly reporting `Health.Status: unhealthy` passes. The previous container is then removed. A process that remains alive while Portainer's API or agent is unusable also passes; delayed crashes after the check are unobserved.

**Reproduction:** Return `{Running: true, Restarting: false, Health: {Status: "unhealthy"}}` for the replacement. The helper reports `Updated` and removes the old container.

**Recommendation:** Respect configured healthchecks and verify control-plane readiness over an appropriate window. Retain a recovery candidate until readiness is established. Preserve the existing warning that an image rollback cannot undo a Portainer database migration.

### A05 — Stack stop/update protection fails open

**Location:** [app/main.py:830](/Users/jworthington/development/restruo/app/main.py:830), lines 830–844; [line 918](/Users/jworthington/development/restruo/app/main.py:918), lines 918–931.  
**Evidence:** Reproduced listing-error bypass; source-confirmed image-ID bypass.

Both safety checks rely on container enumeration. Exceptions are converted to an empty list, so an unavailable inspection becomes permission to stop or redeploy. The stack checks also inspect raw `container.Image`; unlike the standalone path, they do not resolve `sha256:` values back to the original image name. After a tag moves, a Portainer or agent container can therefore evade the substring guard.

**Reproduction:** Make `list_containers()` fail for a stack named `portainer`; the stop endpoint still invokes `set_stack_state(..., running=False)` and returns success. The update endpoint has the same fail-open structure.

**Recommendation:** Refuse the action when safety cannot be established. Resolve image identities consistently and inspect stack declarations as supporting evidence. Do not rely solely on the dashboard disabling a button.

### A06 — Unvalidated upstream identifiers reach HTML injection sinks

**Location:** [app/portainer.py:721](/Users/jworthington/development/restruo/app/portainer.py:721), lines 724–726; [web/index.html:487](/Users/jworthington/development/restruo/web/index.html:487), line 490 and endpoint-ID interpolations at lines 378 and 512; HTML assignment at [line 562](/Users/jworthington/development/restruo/web/index.html:562).  
**Evidence:** Reproduced forwarding of a malicious identifier; source-confirmed HTML sink. No real-browser exploit was executed.

Names and most messages are escaped, but stack IDs and environment IDs are assumed numeric without being validated. A Portainer response can supply an `Id` containing a quote followed by an HTML element/event handler. That value is copied into `data-sid` and `id` attributes and assigned through `innerHTML`.

**Prerequisite and impact:** An attacker must control or impersonate an upstream Portainer response. This is a meaningful trust boundary because one dashboard aggregates several servers, and A08 makes impersonation easier on affected connections. Injected same-origin script could invoke authenticated dashboard actions against other instances. HttpOnly cookies do not stop same-origin requests, and the current CSP only restricts framing, not script execution.

**Recommendation:** Validate all external IDs to the expected types and use DOM properties/`textContent` instead of HTML interpolation. Escape every attribute value regardless of expected type. Add adversarial upstream-response tests and a restrictive script policy compatible with the UI.

### A07 — Instance edits can reroute saved credentials

**Location:** [app/instances.py:107](/Users/jworthington/development/restruo/app/instances.py:107), lines 107–115; [app/main.py:341](/Users/jworthington/development/restruo/app/main.py:341), lines 341–351; test-only guard at [line 388](/Users/jworthington/development/restruo/app/main.py:388).  
**Evidence:** Reproduced through the API with a mock destination.

The connection-test endpoint refuses to reuse a secret for another host, but the save/edit endpoint has no equivalent restriction. Blank secrets preserve the stored credential while `base_url` changes freely. The next instance probe or update scan sends that credential to the new destination.

**Reproduction:** Create an instance at `https://original.example` with a synthetic API key; edit it to `https://other.example` with a blank key; request the instance list. The mock destination receives the original API key. The test endpoint also permits an HTTPS-to-HTTP change on the same host and explicit port because `_host_of()` omits the scheme.

**Prerequisite and impact:** Requires dashboard management access, a stolen session, or A06. The dashboard account already has substantial operational authority, but this additionally exposes reusable Portainer credentials and contradicts the stated destination restriction.

**Recommendation:** Enforce destination binding centrally in the store/validation path, including scheme and port. Require fresh credentials for destination changes and reject silent transport downgrades.

### A08 — UI defaults new instances to unverified TLS

**Location:** [web/index.html:242](/Users/jworthington/development/restruo/web/index.html:242), [line 1071](/Users/jworthington/development/restruo/web/index.html:1071), [line 1088](/Users/jworthington/development/restruo/web/index.html:1088); transport at [app/portainer.py:164](/Users/jworthington/development/restruo/app/portainer.py:164).  
**Evidence:** Source-confirmed.

The certificate-verification checkbox starts unchecked, and resetting the form explicitly sets it to `false`. `formBody()` submits that value, overriding the safer `True` backend default. This disables server authentication even for a Portainer with a valid certificate unless the user opts in.

**Prerequisite and impact:** An attacker able to intercept the Restruo-to-Portainer connection can impersonate Portainer, obtain its API key or login password, alter API responses, and potentially exploit A06. A trusted LAN reduces exposure but does not authenticate servers.

**Recommendation:** Default verification on. Keep any self-signed-certificate exception explicit and persistent per instance; preferably support a configured CA or certificate trust mechanism.

## Medium- and low-priority findings

### A09 — Registry credential trust uses an incorrect domain boundary

**Severity:** Medium. **Evidence:** Reproduced.  
**Location:** [app/registry.py:83](/Users/jworthington/development/restruo/app/registry.py:83), lines 83–101 and 161–169.

`_registrable()` equates a domain with its last two labels. `_realm_trusted("https://attacker.co.uk/token", "registry.victim.co.uk")` returns true; so does the equivalent cross-tenant `github.io` example. A malicious or compromised registry challenge can therefore send a configured login to a separately controlled HTTPS origin.

**Recommendation:** Prefer exact, configured token-service origins with an explicit Docker Hub exception. If registrable-domain logic is necessary, use the Public Suffix List, while recognizing that sibling subdomains can still have different operators. [Public Suffix List explanation](https://publicsuffix.org/learn/).

### A10 — Registry token challenges can trigger arbitrary internal HTTP requests

**Severity:** Medium. **Evidence:** Reproduced with mock transport.  
**Location:** [app/registry.py:152](/Users/jworthington/development/restruo/app/registry.py:152), lines 152–169.

An untrusted realm only loses its Basic credentials; the server still fetches it. A registry response can name `http://127.0.0.1:9000/internal` or another internal service. The reproduced flow made that GET request. HTTPS, destination policy, and internal-address restrictions are not enforced for the request itself.

This requires an image check against an attacker-controlled/compromised registry; it is not an unauthenticated general-purpose proxy exposed directly by a route. It enables server-side request forgery, reachability probing, and potentially side effects on internal GET endpoints. Arbitrary response bodies are not directly returned in full.

**Recommendation:** Validate token endpoints before any request. Use configured allowed origins and an explicit policy for legitimate private registries rather than silently trusting arbitrary challenge destinations.

### A11 — SMTP security typos silently disable encryption

**Severity:** Medium. **Evidence:** Reproduced.  
**Location:** [app/config.py:135](/Users/jworthington/development/restruo/app/config.py:135); [app/notifiers.py:77](/Users/jworthington/development/restruo/app/notifiers.py:77), lines 77–90.

`security` accepts any string. Only `ssl` selects implicit TLS and only `starttls` upgrades the plaintext connection. A value such as `startls`, or a YAML value with trailing whitespace, falls through to ordinary SMTP and still calls `login()`. Against a server offering AUTH PLAIN/LOGIN, credentials can be transmitted without transport encryption.

**Recommendation:** Validate a normalized enum and fail on unknown values. Permit plaintext only through an explicit `none` selection. The Python SMTP API requires `starttls()` to enable TLS on an ordinary SMTP connection. [Python SMTP documentation](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.starttls).

### A12 — Deployment completion can report false success

**Severity:** Medium. **Evidence:** Reproduced five scenarios.  
**Location:** [app/main.py:568](/Users/jworthington/development/restruo/app/main.py:568), lines 568–600 and 775–780; badge clearing at [line 933](/Users/jworthington/development/restruo/app/main.py:933); [web/index.html:780](/Users/jworthington/development/restruo/web/index.html:780), lines 780–786.

The fingerprint includes Docker's human-readable `Status`, so an uptime-text change alone can count as deployment activity. Once any change is followed by two equal polls, the function returns `Redeployed` without checking running state, health, expected service count, or image version. Stable empty, exited, and unhealthy listings all passed the reproduction. A partially recreated stack can likewise settle while other services are still pending.

At timeout, `_await_deploy()` returns explanatory text, but `_update_one()` always sets `ok: true`. Even `Still deploying` clears badges. The browser hides successful message text and shows only elapsed seconds, losing the distinction. Repeated failed polls can also end in the unsupported conclusion that images were already current.

**Recommendation:** Return a structured outcome—verified success, failure, still running, or unknown—and only clear badges after image/readiness verification. Exclude changing display text from identity comparisons. Test the API result and browser behavior, not just the helper's returned sentence.

### A13 — Floating standalone images can be misclassified as digest-pinned

**Severity:** Medium. **Evidence:** Reproduced.  
**Location:** [app/portainer.py:621](/Users/jworthington/development/restruo/app/portainer.py:621), lines 621–635; [app/updates.py:201](/Users/jworthington/development/restruo/app/updates.py:201), lines 201–202 and 354–360.

When a container lists a bare image ID and its old image retains `RepoDigests` but no `RepoTags`, `resolve_image_name()` returns the first digest reference before inspecting `Config.Image`. A container created from `nginx:latest` becomes `nginx@sha256:old`; the checker then labels it pinned and never looks for updates. The existing moved-tag regression tests cover the case where both tags and digests are empty, leaving this branch untested.

**Recommendation:** Prefer the container's original reference when determining whether the operator chose a floating tag. Use image metadata for digest comparison without changing tag-policy semantics.

### A14 — One current replica hides other outdated replicas

**Severity:** Medium. **Evidence:** Reproduced.  
**Location:** [app/updates.py:174](/Users/jworthington/development/restruo/app/updates.py:174), lines 174–192 and 263–264.

All running-image digests are merged into one set; membership of the remote digest is enough to label the whole image current. A stack with one old replica and one new replica reports `up-to-date`. Empty-digest old images or failed inspections can be hidden by a healthy replica as well.

**Recommendation:** Compare each distinct deployed image separately. Report an update if any relevant replica is behind, and unknown if inspection cannot establish its version. Keep explicit policy for expected multi-platform differences.

### A15 — Profile handling skips images also used by active services

**Severity:** Medium. **Evidence:** Reproduced.  
**Location:** [app/portainer.py:114](/Users/jworthington/development/restruo/app/portainer.py:114), lines 114–141; [app/updates.py:321](/Users/jworthington/development/restruo/app/updates.py:321), lines 321–330.

Inactive profiles are reduced to a set of image names. If an optional service and an active service both use `nginx:latest`, the image is skipped for the entire stack as `not-deployed`, even with a matching active container. The general assumption that every profiled service is inactive also ignores evidence of actual deployed containers.

**Recommendation:** Determine activity per service, account for actual containers, and only skip an image when no active service uses it.

### A16 — Compose parsing silently misses or misinterprets images

**Severity:** Medium. **Evidence:** Reproduced empty-file, inline-YAML, and interpolation cases.  
**Location:** [app/portainer.py:27](/Users/jworthington/development/restruo/app/portainer.py:27), lines 43–111.

Image extraction is a line-oriented regular expression rather than structural parsing. Valid inline YAML such as `services: {web: {image: "nginx:latest"}}` produces no images. `stack_images()` returns immediately for an empty extracted list because `all([])` is true, so missing stack files and unsupported syntax do not fall back to known containers. Images inside unrelated YAML sections can conversely be mistaken for services.

Interpolation also conflates required-value errors with defaults: `nginx:${TAG:?latest}` becomes `nginx:latest` when unset. It treats `${PREFIX-default}` with an explicitly empty value as unset and does not fully handle nested or alternative expressions. These can produce false pinned/local/unknown classifications or check the wrong reference.

**Recommendation:** Parse service definitions structurally, use the live container reference as authoritative when appropriate, and preserve uncertainty for unsupported expressions. Implement or explicitly limit Compose interpolation semantics. [Docker Compose interpolation rules](https://docs.docker.com/reference/compose-file/interpolation/).

### A17 — Read failures become apparently healthy or unmonitored state

**Severity:** Medium. **Evidence:** Reproduced dashboard and image-inspection cases; additional branches source-confirmed.  
**Location:** [app/main.py:436](/Users/jworthington/development/restruo/app/main.py:436), lines 436–479; [app/updates.py:186](/Users/jworthington/development/restruo/app/updates.py:186), lines 186–191, 220–243, 290–294, and 314–317.

Failed environment/container enumeration is converted to an empty result while the instance remains reachable. Standalone containers disappear, and stacks get empty `downNames`. Failed image inspection is swallowed and may be reported as a locally built image. Failed file fetches produce zero checked images through A16. The dashboard does not consistently surface the per-instance checker `error` either.

**Impact:** A control-plane or permission failure can look like no problems or no updates, particularly on mobile where image details are hidden.

**Recommendation:** Keep unavailable, empty, and successful states distinct. Surface per-environment errors and preserve last-known data with a stale marker. Never infer `local`, healthy, or fully checked from failed reads.

### A18 — Stack ownership matching hides containers on other environments

**Severity:** Medium. **Evidence:** Reproduced matcher behavior; source-confirmed callers.  
**Location:** [app/main.py:491](/Users/jworthington/development/restruo/app/main.py:491), lines 491–502; [app/updates.py:343](/Users/jworthington/development/restruo/app/updates.py:343), lines 343–369; [app/portainer.py:642](/Users/jworthington/development/restruo/app/portainer.py:642).

The set of Portainer stack names is built across the entire instance, then applied to every environment. An external Compose project on environment B disappears if its project name matches a Portainer-managed stack on environment A. It is neither listed nor checked as standalone, and it does not belong to A's stack container list.

**Recommendation:** Match ownership by environment plus project name. Build a separate managed-name set for each endpoint.

### A19 — Notification bookkeeping loses failed mail and repeats after outages

**Severity:** Medium. **Evidence:** Reproduced both cases.  
**Location:** [app/updates.py:428](/Users/jworthington/development/restruo/app/updates.py:428), especially lines 464–473; [app/notifiers.py:105](/Users/jworthington/development/restruo/app/notifiers.py:105), lines 105–111.

Findings are marked notified and persisted before delivery. Email delivery exceptions are swallowed, and the next check sees the same finding as already announced, so a transient mail failure can suppress the notification indefinitely. Conversely, an unavailable instance has no results, causing its notification keys to be forgotten; recovery re-mails the unchanged updates.

The deduplication key also lacks the remote digest. A newer release under the same still-outdated tag is not a new event, which may be intentional, but should be documented or adjusted if notifications are meant to represent releases.

**Recommendation:** Track successful delivery per notifier, retain retryable failures, and preserve prior knowledge for checks that failed. Distinguish an observed resolution from missing observations.

### A20 — Automatic dashboard refresh never refreshes update results

**Severity:** Medium. **Evidence:** Source-confirmed.  
**Location:** [web/index.html:622](/Users/jworthington/development/restruo/web/index.html:622), lines 622–643, 661–665, and 753–756.

`autoTick()` calls `refresh()`, which fetches only `/api/stacks`. The separate `updates` object is refreshed on initial load, manual actions, or leaving settings, but not on the automatic cadence. A scheduled check can discover updates and send mail while an open dashboard displays old badges indefinitely. A first load during a background scan can leave `checking registries…` stale for the same reason.

**Recommendation:** Fetch cached `/api/updates` alongside each state refresh, and poll while a check is running. This does not require repeatedly scanning registries.

### A21 — Reused instance IDs can redirect stale actions to another server

**Severity:** Medium. **Evidence:** Reproduced ID reuse; stale-action consequence is source-confirmed.  
**Location:** [app/instances.py:92](/Users/jworthington/development/restruo/app/instances.py:92), lines 92–98; action lookup at [app/main.py:517](/Users/jworthington/development/restruo/app/main.py:517).

IDs are allocated as the maximum currently present plus one. Deleting the highest ID, or all instances, allows reuse. A second browser tab can retain the deleted server's rows and submit an action after the ID is assigned to another server. Stack IDs commonly overlap across Portainer installations, so the request can operate on an unintended stack. Cached update/notification state is also keyed by this reused identity.

**Recommendation:** Use nonreusable IDs, such as UUIDs or a persisted monotonic sequence. Invalidate cached results when an instance is removed or its destination changes, and bind actions to an instance revision where appropriate.

### A22 — Failed store writes leave memory and disk inconsistent

**Severity:** Medium. **Evidence:** Reproduced with simulated disk-full error.  
**Location:** [app/instances.py:95](/Users/jworthington/development/restruo/app/instances.py:95), lines 95–151.

Add, edit, reorder, delete, and seed mutate `_records` before `_save()` succeeds. A failed write returns an error but leaves the mutation live in memory. The API then skips `ClientManager.refresh()`, so records, active clients, and disk may each describe different state. Restart can resurrect a supposedly removed entry or discard a seemingly applied edit.

**Recommendation:** Construct a candidate state, persist it successfully, and only then publish it in memory. Handle storage failures with a clear response. Consider file/directory fsync if crash durability is a requirement; rename alone does not guarantee it.

### A23 — Client replacement can interrupt active operations

**Severity:** Medium. **Evidence:** Reproduced closure of a client retained by in-flight work; live transport interruption not exercised.  
**Location:** [app/instances.py:169](/Users/jworthington/development/restruo/app/instances.py:169), lines 175–186; [app/portainer.py:173](/Users/jworthington/development/restruo/app/portainer.py:173); reconnect calls at [app/main.py:313](/Users/jworthington/development/restruo/app/main.py:313).

Editing even an instance's display name creates a new client and immediately closes the previous one. Active deploy/check jobs retain the old client. A failed listing also calls `reconnect()`, closing the shared connection pool while other requests may be using it. This can disrupt a deploy that has already reached Portainer and then misreport it as failed or lose monitoring.

**Recommendation:** Drain or reference-count old clients, coordinate reconnection, and distinguish display-only edits from connection changes. Block or safely defer destination/credential changes during privileged jobs.

### A24 — Operation locks do not cover conflicting actions

**Severity:** Medium. **Evidence:** Reproduced stop-during-update bypass; other overlaps source-confirmed.  
**Location:** [app/main.py:524](/Users/jworthington/development/restruo/app/main.py:524), [line 818](/Users/jworthington/development/restruo/app/main.py:818), [line 861](/Users/jworthington/development/restruo/app/main.py:861), [line 1000](/Users/jworthington/development/restruo/app/main.py:1000); browser ordering at [web/index.html:1034](/Users/jworthington/development/restruo/web/index.html:1034).

The lock only prevents duplicate updates with the same tuple. Start/stop and prune ignore it. Agent replacement has a different key from stacks deployed through that agent, so another tab or API caller can replace the transport while a stack is deploying. The browser's “agents last” ordering only applies within that one bulk-update operation.

**Reproduction:** Populate `in_flight` with `("stack", 1, 1)` and call the stack stop route; it proceeds successfully.

**Recommendation:** Coordinate incompatible mutations per target and per environment. Agent/Portainer replacement needs a broader exclusion boundary. Keep harmless independent operations concurrent. These in-memory locks also only coordinate a single application worker.

### A25 — Omitting environment ID can target the wrong cloned host

**Severity:** Medium. **Evidence:** Reproduced.  
**Location:** [app/main.py:784](/Users/jworthington/development/restruo/app/main.py:784), lines 794–806; optional parameters at lines 893, 900, and 944.

The UI now sends `endpointId`, but the API still allows omission. `_find_container()` returns the first matching ID across environments, even though the code and tests explicitly recognize cloned hosts with identical container IDs. A script following the basic documented route can start, stop, or update the wrong clone.

**Recommendation:** Require an environment for mutations, or resolve all matches and reject ambiguity rather than selecting the first. Document the requirement in the API table.

### A26 — Example `.env` settings are not passed into the container

**Severity:** Medium. **Evidence:** Source-confirmed against Compose environment semantics.  
**Location:** [.env.example:1](/Users/jworthington/development/restruo/.env.example:1); [docker-compose.yml:14](/Users/jworthington/development/restruo/docker-compose.yml:14), lines 14–19.

The example invites copying settings to `.env`, including SMTP and registry credentials. The Compose service passes only `DASHBOARD_PASSWORD`; its other environment entries are commented out and there is no `env_file`. Setting `RESTRUO_SMTP_USER`, `RESTRUO_SMTP_PASSWORD`, or registry auth in `.env` alone therefore does not configure the container. Even the commented username/title examples use fixed values rather than variable interpolation.

**Recommendation:** Explicitly wire supported variables into the service, or use a documented `env_file` approach with intentional precedence. Verify the rendered Compose configuration with synthetic values. Compose's interpolation environment is distinct from the container's environment. [Docker environment configuration](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

### A27 — Validation responses can echo submitted secrets

**Severity:** Low. **Evidence:** Reproduced.  
**Location:** [app/main.py:208](/Users/jworthington/development/restruo/app/main.py:208), lines 208–212 and 264–273; regression coverage at [tests/test_audit_fixes.py:285](/Users/jworthington/development/restruo/tests/test_audit_fixes.py:285).

`hide_input_in_errors=True` hides input in formatted Pydantic exception strings, but does not sanitize FastAPI's default structured validation response. Posting `{"password":"SYNTHETIC_SECRET"}` without a username to `/api/login` returns a 422 response containing that password in the error's input object. Similar request-body failures can echo instance credentials.

This reflects caller-supplied data, not an extraction of another user's stored secret, so severity is low. It still contradicts the broad no-secret-error guarantee and can expose credentials to response logging or diagnostic captures.

**Recommendation:** Add a request-validation handler that omits input values and test missing fields, wrong types, and malformed bodies through the HTTP API.

### A28 — Malformed session cookies cause server errors

**Severity:** Low. **Evidence:** Reproduced without authentication.  
**Location:** [app/auth.py:48](/Users/jworthington/development/restruo/app/auth.py:48), lines 48–57.

The validator bounds total length but does not enforce the ASCII decimal/hex token format. A quoted cookie with an octal escape yielding a non-ASCII signature reaches `hmac.compare_digest()` as a Unicode string and raises `TypeError`, producing HTTP 500. Some Unicode characters also satisfy `isdigit()` without being accepted by `int()`.

**Recommendation:** Validate the entire token against the exact expected grammar before conversion/comparison. Reject malformed cookies as unauthenticated. The demonstrated impact is per-request error/log amplification, not a proven process-wide denial of service.

### A29 — Secrets are written before restrictive permissions are applied

**Severity:** Low. **Evidence:** Reproduced temporary-file mode under umask 022; session-secret path source-confirmed.  
**Location:** [app/instances.py:85](/Users/jworthington/development/restruo/app/instances.py:85), lines 85–90; [app/auth.py:35](/Users/jworthington/development/restruo/app/auth.py:35), lines 35–37.

The instance temporary file and newly generated session secret are created using default permissions, filled with secrets, and only then chmodded to 0600. Under a common umask, the temporary instance file was 0644 before chmod. A local user who can traverse the data directory can potentially read during this window; a crash between write and chmod can extend it.

**Recommendation:** Create files as 0600 from the first open, use unique exclusive temporary files, and restrict the parent directory appropriately. The current final-file permission tests only inspect the final state.

### A30 — Concurrency and retained state lack consistent resource bounds

**Severity:** Low. **Evidence:** Source-confirmed; no load benchmark performed.  
**Location:** [app/main.py:462](/Users/jworthington/development/restruo/app/main.py:462), lines 462–503; [app/updates.py:298](/Users/jworthington/development/restruo/app/updates.py:298), [line 408](/Users/jworthington/development/restruo/app/updates.py:408); [app/auth.py:76](/Users/jworthington/development/restruo/app/auth.py:76); [app/registry.py:112](/Users/jworthington/development/restruo/app/registry.py:112).

The scheduled checker bounds some work to six concurrent operations per instance, but dashboard stack-file fetches, environment enumeration, and standalone image-name resolution fan out without equivalent limits. Each browser repeats the work. Manual scans queue behind a lock and then repeat the entire scan instead of sharing the result of an already-running check.

Login failure entries are only expired when that same address is revisited, and registry token entries are never evicted. Job pruning happens only when starting another job and only removes completed jobs. The dashboard also repeatedly performs linear searches while rebuilding all rows, creating avoidable quadratic work on larger inventories.

**Recommendation:** Apply consistent per-instance/global concurrency limits, coalesce scans, cache/index short-lived display data, and add periodic bounded eviction. Measure latency and peak tasks against realistic larger installations before selecting limits.

### A31 — Local package installer has published vulnerabilities

**Severity:** Medium for development/install operations; not an established dashboard runtime exploit. **Evidence:** Environment scan and upstream corroboration.  
**Location:** `/Users/jworthington/development/restruo/.venv/` installed package metadata; build-time installation at [Dockerfile:6](/Users/jworthington/development/restruo/Dockerfile:6).

`pip-audit` scanned 31 installed packages and returned 12 advisory records, which reduce to **six distinct advisories, all for pip 25.2**. It reported no advisories for the other 30 packages in that environment. Duplicate advisory records must not be counted as separate vulnerabilities.

| Advisory | Scanner-reported fixed version | Applicability |
|---|---|---|
| CVE-2025-8869 / GHSA-4xh5-x5gv-qwph | 25.3 | Fallback tar extraction issue; the observed Python 3.13.9 has the newer extraction mechanism, so the advisory's fallback prerequisite does not apply here |
| CVE-2026-1703 / GHSA-6vgw-5pg2-w6jp | 26.0 | Malicious wheel extraction |
| CVE-2026-3219 / GHSA-58qw-9mgm-455v | 26.1 | Ambiguous tar/ZIP package archive handling |
| CVE-2026-6357 / GHSA-jp4c-xjxw-mgf9 | 26.1 | Deferred imports during post-install self-update checks |
| CVE-2026-8643 / GHSA-wf93-45jw-7689 | 26.1.2 | Entry-point path traversal during installation |
| CVE-2026-13346 / GHSA-qwm4-qh6w-59xr | 26.2 / 26.2.0 | Malicious package-index URL prerequisite; especially relevant to binary-only downloads |

The scanner results concern package installation/download paths. I did not establish that the application exposes these paths over its API. The deployed container's pip and OS package versions were not inspected and must not be assumed to match this local environment.

**Recommendation:** Refresh development/build installer tooling to a release covering the reported fixes, and scan the actual published image/SBOM separately. Upstream confirms the entry-point issue and its repair: [Python security announcement](https://mail.python.org/archives/list/security-announce@python.org/thread/YV63UET5D3OOJY7O4M5XCVYO2YM4NBYJ/), [pip patch](https://github.com/pypa/pip/pull/14000). The full six-advisory inventory above is from the package scan; no automatic fixes were applied.

### A32 — Browser tracking expires before supported server jobs finish

**Severity:** Low. **Evidence:** Source-confirmed.  
**Location:** [web/index.html:811](/Users/jworthington/development/restruo/web/index.html:811), lines 811–825; [app/main.py:656](/Users/jworthington/development/restruo/app/main.py:656), lines 707–720; [app/portainer.py:22](/Users/jworthington/development/restruo/app/portainer.py:22).

The browser gives up polling after ten minutes, while the helper watcher allows fifteen minutes after helper preparation, which itself includes a potentially long image pull. A normal long-running server job can therefore be shown as failed while it is still executing. Individual fetches also have no browser abort deadline, so the nominal ten-minute bound is not a strict bound on a hung request.

**Recommendation:** Expose job state and a server-selected tracking deadline, distinguish lost tracking from failure, and allow reconnection to active jobs. Apply bounded request timeouts separately from job duration.

## Additional maintenance and design observations

These are lower-priority improvements or explicit operating limits, not additional high-confidence security exploits.

1. **Reproducibility and dependency declaration.** [requirements.txt:1](/Users/jworthington/development/restruo/requirements.txt:1) uses broad ranges without a lock or hashes. The application directly imports Pydantic but relies on FastAPI to install it transitively. The [Dockerfile:1](/Users/jworthington/development/restruo/Dockerfile:1) base image and [CI actions](/Users/jworthington/development/restruo/.github/workflows/docker.yml:13) use mutable tags. Test and image jobs resolve dependencies independently, so the published image need not contain precisely the tested set. Record resolved dependencies and image provenance, declare direct dependencies, and use controlled immutable inputs where appropriate. This is a supply-chain and reproducibility concern, not proof that the current dependencies are malicious.

2. **Test coverage misses the highest-risk semantics.** The suite thoroughly exercises many happy paths and prior regressions, but the mock Engine does not model volume allocation, health readiness, static-network conflicts, or partial recovery failures. Deployment tests assert returned text rather than API success flags and badge behavior. There are no browser tests, and CI has no container startup smoke test. Add targeted behavior tests for the findings, plus disposable Engine and browser integration tests. The local test runtime was Python 3.13.9, while CI and Docker specify 3.14; this audit did not establish the production-image result.

3. **Confirmed vestigial test code.** [tests/test_manual_check.py:25](/Users/jworthington/development/restruo/tests/test_manual_check.py:25) defines unused `fake_instance_results`; [tests/test_audit_fixes.py:394](/Users/jworthington/development/restruo/tests/test_audit_fixes.py:394) defines unused `gate`. Five unused imports were found: `asyncio` in [test_deploy_completion.py:10](/Users/jworthington/development/restruo/tests/test_deploy_completion.py:10); `asyncio` and `pytest` in [test_email.py:3](/Users/jworthington/development/restruo/tests/test_email.py:3); `os` and `load_config` in [test_moving_tags.py:9](/Users/jworthington/development/restruo/tests/test_moving_tags.py:9). No clearly unused production function was established. Vulture's warnings about FastAPI route handlers and Pydantic validators were rejected as framework false positives.

4. **Documentation and comments have drifted.** The update-module docstring still says only latest/untagged images are checked; defaults now include multiple channel tags. `config.example.yaml` narrows floating tags back to `[latest]` when mounted, despite broader defaults described in the README. The README's blanket API-auth sentence omits public `/api/ui-config` and automatically generated FastAPI documentation routes. The old build specification describes an earlier architecture and is appropriately ignored, but production docstrings still cite it. Reconcile guarantees such as “never returned,” rollback, and update completion with actual behavior.

5. **Accessibility gaps.** Collapsible instance headers and the home control are clickable `div` elements without keyboard handling; the modal lacks focus management, focus trapping/restoration, Escape handling, and an `aria-labelledby` link. Login controls rely on placeholders without persistent labels. Busy/result messages are not consistently exposed as live status. These are source-confirmed; assistive-technology testing was not performed. Prefer native controls and an accessible dialog implementation.

6. **Job lifecycle and deployment topology.** Jobs, locks, tokens, and result caches are process-local. There is no shared coordination for multiple Uvicorn workers/replicas, and JSON store locking is only in-process. Shutdown cancels the checker without awaiting it and closes clients without draining background jobs. The default command uses one worker, so this is a limitation to document rather than evidence of a default multiworker failure. Restruo self-updates can lose job history, especially during a bulk update.

7. **Additional configuration validation would prevent avoidable failures.** Instance URLs are arbitrary strings and unsupported schemes can be saved before client construction fails. YAML `registry_auth` values without a colon cause startup indexing errors; environment parsing silently drops malformed entries. SMTP ports and YAML refresh values have inconsistent validation. Switching authentication mode can retain credentials no longer used. Validate on save/startup and discard retired secrets deliberately.

8. **Frontend state/error handling is inconsistent.** Manual refresh, registry-result rendering, searching, and delayed power-action refresh can replace DOM nodes during jobs even though `busyCount` only suppresses automatic refresh. Buttons and result messages are held by element reference, so their results can land on detached nodes. Reorder/delete handlers do not consistently surface unsuccessful responses. Sign-out shows the login screen even if the logout request fails, and does not clear cached data. Use explicit application/job/auth state instead of relying on current DOM elements.

9. **Polling and logging are uneven.** The instance-list route probes every server even when the settings page only needs metadata, and all-instance responses wait for the slowest endpoint. Several user-facing errors use bare `str(exc)`, which is empty for some timeout exceptions despite an existing descriptive-error helper. Mutating operations lack a durable audit trail even though failed logins are logged. Prefer separate metadata/health reads and consistent, redacted operation logging.

10. **Helper compatibility needs explicit boundaries.** It assumes a Linux host with `/var/run/docker.sock`; it does not negotiate Docker API versions, pass private-registry authentication for the target image pull, or verify every inspection field needed for a faithful recreate. Static IPs, `AutoRemove`, container namespace dependencies, and image-inherited settings need Engine integration coverage. These are compatibility risks requiring real topology tests, not all asserted as independently reproduced defects.

## Validation results and limitations

| Check | Result | Interpretation |
|---|---|---|
| Manual source review | All first-party executable/configuration text and all tests read | No sampled-file-only review |
| Existing pytest suite | 180 passed initially; one SMTP test blocked by loopback socket restriction, then passed individually with permission | All 181 existing tests passed across those runs |
| Additional audit reproductions | 37 passed | Confirmed defect behavior and supporting code paths using synthetic data |
| JavaScript syntax | Passed `node --check` | Syntax only; no complete browser interaction test |
| Entrypoint syntax | Passed shell syntax check | No Docker image build or runtime smoke test |
| Bandit 1.9.4 | Nine low-severity findings | Broad exception handling; manually triaged, not nine independent vulnerabilities |
| Ruff 0.16.10, isolated F/E9 rules | Five unused imports, all in tests | No production findings under those selected rules |
| Vulture 2.16 | Two confirmed dead test declarations plus framework false positives | No justified removal of production routes/validators |
| pip-audit 2.10.1 | 31 packages; six unique advisories for local pip 25.2; other packages had no reported advisories | No guarantee about unknown vulnerabilities or deployed-image packages |
| Source integrity | Original tracked file hashes and clean Git status verified | Audit deliverable and temporary checks kept outside the project |

Observed local versions were FastAPI 0.141.1, Starlette 1.3.1, Pydantic 2.13.4, Uvicorn 0.51.0, HTTPX 0.28.1, PyYAML 6.0.3, pytest 9.1.1, and pytest-asyncio 1.4.0. Tests emitted a Starlette deprecation warning about the HTTPX TestClient integration; that warning did not fail the tests.

The audit does not certify absence of vulnerabilities. It did not penetrate a running deployment, test actual Portainer/Docker version combinations, inspect production credentials or reverse-proxy settings, benchmark production workloads, review every third-party library line, scan the published image's OS packages, or search historical commits for deleted secrets. The listed guarantees are limited to the reviewed snapshot and stated validation.

## Suggested remediation order

1. Correct helper data preservation, ownership checks, readiness verification, and rollback (A01–A04), with disposable real-Engine tests.
2. Close the upstream-to-dashboard injection and credential-routing paths; enable TLS verification by default and fail closed on unsafe stack actions (A05–A08).
3. Fix deployment outcome reporting and update-detection blind spots before relying on green badges or bulk updates (A12–A18, A20).
4. Correct notification delivery state, instance identity/persistence, and mutation coordination (A19, A21–A25).
5. Tighten registry/SMTP boundaries, deployment examples, dependency tooling, resource controls, and smaller maintenance issues.

No remediation was applied as part of this audit.
