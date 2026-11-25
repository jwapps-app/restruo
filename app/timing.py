"""Every interval and deadline, in one place.

They depend on each other — a client must outlive the longest job that can
hold it, a job record must outlive the page's patience — and a number in one
module with its reason in a comment in another is how those drift apart.
What must exceed something else is computed from it; tests/test_timing.py
asserts the ordering.

Modules read these as `timing.NAME` at call time, so a test can shorten one.
"""

# --- talking to Portainer ---------------------------------------------------

# Reads (list/get) should fail fast; a redeploy legitimately takes minutes
# because Portainer pulls images before recreating containers.
READ_TIMEOUT = 10.0
REDEPLOY_TIMEOUT = 600.0
# A connection pool replaced after a failed probe stays open this long, so a
# deploy already in flight on it can finish.
POOL_RETIRE_SECONDS = REDEPLOY_TIMEOUT + 60

# --- watching a stack redeploy ----------------------------------------------

DEPLOY_POLL_SECONDS = 2.0
DEPLOY_SETTLE_POLLS = 3      # unchanged this many times running = settled
DEPLOY_TIMEOUT_SECONDS = 600.0

# --- watching a recreated container -----------------------------------------

CONTAINER_POLL_SECONDS = 2.0
CONTAINER_POLLS = 30           # a minute to prove itself
CONTAINER_STABLE_POLLS = 4     # up, and not restarting, this many polls running

# --- the helper that replaces Portainer or an agent -------------------------

HELPER_POLL_SECONDS = 3.0
# How long the dashboard watches a helper before saying it lost track.
# Known gap: the helper's own pull (HELPER_PULL_READ_SECONDS between chunks,
# no overall bound) can outlast this on a slow link; the helper then finishes
# unobserved.
HELPER_TIMEOUT_SECONDS = 900.0

# Inside the helper container (app/helper.py):
# Let the request that started it finish its round trip before the container
# carrying that request is stopped.
HELPER_STARTUP_GRACE_SECONDS = 3.0
HELPER_STOP_TIMEOUT_SECONDS = 20
HELPER_CONNECT_SECONDS = 30.0
HELPER_PULL_READ_SECONDS = 1800.0   # a pull can be slow
READY_POLL_SECONDS = 2.0
READY_POLLS = 45            # 90 s for a healthcheck to pass
READY_STABLE_POLLS = 4      # running (and healthy, if checked) this long in a row

# --- jobs ---------------------------------------------------------------------

# A redeploy can run for minutes. Answer inline when it finishes quickly, and
# otherwise hand the page a job id to poll — an HTTP request that stays open
# for four minutes is fine on a LAN and dead on arrival behind most proxies,
# which cut it off around 100 s while the deploy carries on regardless.
JOB_SYNC_WAIT_SECONDS = 25.0
# How long the page keeps asking about a job (sent to it in /api/ui-config).
# Longer than a helper can be watched; past it the page says tracking was
# lost rather than calling the update failed.
PAGE_JOB_WAIT_SECONDS = HELPER_TIMEOUT_SECONDS + 25 * 60
# The record outlives the page's patience.
JOB_RETENTION_SECONDS = PAGE_JOB_WAIT_SECONDS + 20 * 60

# --- clients ------------------------------------------------------------------

# A client replaced by an edit is closed only once a helper started on it —
# the longest single wait — can be over.
RETIRE_AFTER_SECONDS = HELPER_TIMEOUT_SECONDS + 5 * 60

# --- registries and mail ------------------------------------------------------

REGISTRY_TIMEOUT = 15.0
SMTP_TIMEOUT = 30
