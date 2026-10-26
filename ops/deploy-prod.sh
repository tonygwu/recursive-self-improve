#!/bin/bash
# Gated deployment of origin/main into the production checkout (repo-prod),
# invoked by launchd (com.tonygwu.self-improve.deploy) at 01:00, ahead of the
# 02:30 nightly. Tests run in a SEPARATE staging clone; repo-prod is moved only
# after the whole suite passes and the live schema is already current.
#
# This script never edits repo-prod's working tree. It only fast-forwards it.
#
# SELF-MODIFICATION GUARD. This script lives inside the checkout it updates, and
# bash reads a script incrementally from disk as it executes. A `git merge` that
# rewrites this file mid-run would make bash resume at a byte offset in the NEW
# file and execute garbage. Section 0 therefore copies the script to a temporary
# file and re-execs from there, so the file bash is reading can never be the file
# git is writing. Do not remove that block or move the merge earlier.
#
# launchd runs jobs with a minimal PATH that excludes ~/.local/bin and
# /opt/homebrew/bin, so every command here is an absolute path. Same trap that
# `ops/run-nightly.sh` documents.
#
# LOCKING. This takes the SAME lock as run-nightly.sh (${STATE_DIR}/run.lock,
# mkdir-based with stale-PID reclaim). Two reasons: a deploy must never swap code
# under a running pipeline, and a deploy that overruns into 02:30 must make the
# nightly skip rather than race. A skipped night is the safe failure.
#
# -u and pipefail, deliberately NOT -e, matching run-nightly.sh: every failure
# below is handled explicitly so the log names a cause instead of recording a
# bare non-zero exit.
set -u -o pipefail

# --- 0. self-modification guard ----------------------------------------------
# Runs before anything else and before `set` matters, so the re-exec costs one
# fork and nothing else. SI_DEPLOY_SOURCE_ROOT is exported here because after the
# exec, BASH_SOURCE names the temporary copy and can no longer locate repo-prod.
if [ -z "${SI_DEPLOY_REEXEC:-}" ]; then
    _SELF_DIR="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
    _SELF="${_SELF_DIR}/$(/usr/bin/basename "${BASH_SOURCE[0]}")"
    SI_DEPLOY_SOURCE_ROOT="${SI_PROD_REPO:-$(/usr/bin/dirname "${_SELF_DIR}")}"
    export SI_DEPLOY_SOURCE_ROOT
    # An explicit template, because GNU mktemp rejects `-t si-deploy-prod`
    # ("too few X's") while BSD mktemp accepts it; both accept this form. One
    # difference: with TMPDIR unset this uses /tmp, where macOS `-t` used the
    # per-user temporary folder. mktemp still creates the file exclusively, 0600.
    _COPY="$(/usr/bin/mktemp "${TMPDIR:-/tmp}/si-deploy-prod.XXXXXX")" || exit 1
    if ! /bin/cp "${_SELF}" "${_COPY}"; then
        echo "deploy setup failed: could not copy ${_SELF} to ${_COPY}" >&2
        /bin/rm -f "${_COPY}"
        exit 1
    fi
    export SI_DEPLOY_REEXEC=1
    export SI_DEPLOY_SELF_COPY="${_COPY}"
    exec /bin/bash "${_COPY}" "$@"
fi

if [ $# -ne 0 ]; then
    echo "usage: ${0##*/}" >&2
    exit 2
fi

# Makes the guard observable, in the launchd log and to the test suite: the file
# bash is reading must be the temporary copy, never the checkout's own
# ops/deploy-prod.sh. BASH_SOURCE, not SI_DEPLOY_SELF_COPY — the copy's path is
# exported either way, so only the actually-running file distinguishes them.
echo "deploy: executing ${BASH_SOURCE[0]} (production checkout ${SI_DEPLOY_SOURCE_ROOT})"

# --- 1. resolved paths -------------------------------------------------------
# The ${VAR:-default} form exists so tests can point these at fakes and EXECUTE
# this script, rather than asserting on its source text. launchd sets none of
# them, so the defaults are what actually runs in production.
STATE_DIR="${SI_STATE_DIR:-${HOME}/.self-improve}"
LOCK_DIR="${SI_LOCK_DIR:-${STATE_DIR}/run.lock}"
PROD_REPO="${SI_PROD_REPO:-${SI_DEPLOY_SOURCE_ROOT}}"
# The stage sits BESIDE the production checkout, not inside the state directory.
# tests/conftest.py forbids reads under ~/.self-improve by PREFIX, so a checkout
# placed there makes every test that reads its own source tree fail the
# hermeticity guard. Measured: 975 errors and a blocked deploy on 2026-09-20.
# The name is dot-prefixed and unnumbered on purpose. routing._REPO_N_RE matches
# only `repo-<digits>`, so this can never be mistaken for a working copy that
# automation writes to.
STAGE_DIR="${SI_DEPLOY_STAGE:-$(/usr/bin/dirname "${PROD_REPO}")/.deploy-stage}"
DEPLOY_DIR="${STATE_DIR}/deploy"
LOG_DIR="${STATE_DIR}/logs"
TEST_LOG="${LOG_DIR}/deploy-tests.log"
GIT="${SI_GIT:-/usr/bin/git}"
PYTHON3="${SI_PYTHON3:-/usr/bin/python3}"
DEPLOY_REF="${SI_DEPLOY_REF:-origin/main}"
REMOTE="${SI_DEPLOY_REMOTE:-origin}"
BRANCH="${SI_DEPLOY_BRANCH:-main}"

PATH="${SI_PATH_EXTRA:-${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin}:${PATH}"
export PATH

UV="${SI_UV:-$(command -v uv)}"
if [[ "${UV}" != /* || ! -x "${UV}" ]]; then
    echo "deploy setup failed: uv was not found as an absolute executable; install uv or set SI_UV to its absolute path"
    exit 1
fi

/bin/mkdir -p "${DEPLOY_DIR}" "${LOG_DIR}"

STARTED_AT="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
FROM_SHA=""
TO_SHA=""
TESTS_SUMMARY=""

# --- 2. status reporting -----------------------------------------------------
# Every exit path goes through emit(). `status` is a closed taxonomy, not free
# text, so the history file can be counted: a run is attempted once, and ends in
# exactly one of these. Timestamps are UTC because the rest of the state database
# is UTC; the local wall clock never decides a recorded time here.
#
#   deployed                  repo-prod fast-forwarded to TO_SHA
#   noop_already_current      repo-prod already at the candidate
#   blocked_dirty_tree        repo-prod has local modifications
#   blocked_not_fast_forward  candidate is not a descendant of repo-prod HEAD
#   blocked_tests_failed      the gate suite did not pass
#   blocked_pending_migrations  live schema is behind the candidate's code
#   failed_lock               another run/deploy holds the pipeline lock
#   failed_precondition       repo-prod or the remote is not in a usable state
#   failed_stage              the staging clone could not be prepared
#   failed_merge              the fast-forward or post-merge verification failed
emit() {
    local status="$1" detail="$2" code="$3"
    "${PYTHON3}" - "$@" <<'PY' >> "${DEPLOY_DIR}/history.jsonl"
import json, os, sys
record = {
    "started_at": os.environ["SI_DEPLOY_STARTED_AT"],
    "finished_at": os.environ["SI_DEPLOY_FINISHED_AT"],
    "status": sys.argv[1],
    "detail": sys.argv[2],
    "exit_code": int(sys.argv[3]),
    "prod_repo": os.environ["SI_DEPLOY_PROD_REPO"],
    "from_sha": os.environ["SI_DEPLOY_FROM_SHA"] or None,
    "to_sha": os.environ["SI_DEPLOY_TO_SHA"] or None,
    "tests_summary": os.environ["SI_DEPLOY_TESTS_SUMMARY"] or None,
}
print(json.dumps(record))
with open(os.environ["SI_DEPLOY_LAST"], "w") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\n")
PY
    echo "deploy ${status}: ${detail}"
}

finish() {
    local status="$1" detail="$2" code="$3"
    # Explicit `export`, not `VAR=x emit ...`. Bash does not reliably place
    # assignment-prefixed variables into the environment of commands run INSIDE
    # a shell function, so the prefix form would hand python an empty env and
    # raise KeyError. Exporting is unambiguous and is what the tests exercise.
    export SI_DEPLOY_STARTED_AT="${STARTED_AT}"
    export SI_DEPLOY_FINISHED_AT="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
    export SI_DEPLOY_PROD_REPO="${PROD_REPO}"
    export SI_DEPLOY_FROM_SHA="${FROM_SHA}"
    export SI_DEPLOY_TO_SHA="${TO_SHA}"
    export SI_DEPLOY_TESTS_SUMMARY="${TESTS_SUMMARY}"
    export SI_DEPLOY_LAST="${DEPLOY_DIR}/last-deploy.json"
    emit "${status}" "${detail}" "${code}"
    exit "${code}"
}

cleanup() {
    [ -n "${SI_DEPLOY_SELF_COPY:-}" ] && /bin/rm -f "${SI_DEPLOY_SELF_COPY}"
    [ -n "${LOCK_HELD:-}" ] && /bin/rm -rf "${LOCK_DIR}"
    return 0
}
LOCK_HELD=""
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 3. pipeline lock --------------------------------------------------------
# Identical protocol to run-nightly.sh. Kept byte-compatible on purpose: both
# scripts write a `pid` file inside the lock directory and reclaim a lock whose
# recorded PID is dead. tests/test_deploy_prod.py pins that both agree.
# ps prints lstart in the caller's locale and time zone. The nightly and the
# deploy job may run under different ones, so both write and compare it in one
# fixed format.
start_time_of() {
    LC_ALL=C TZ=UTC /bin/ps -o lstart= -p "$1" 2>/dev/null
}

acquire_lock() {
    if /bin/mkdir "${LOCK_DIR}" 2>/dev/null; then
        echo "$$" > "${LOCK_DIR}/pid"
        # The start time tells a reused PID from the process that took the lock.
        start_time_of "$$" > "${LOCK_DIR}/started"
        return 0
    fi
    return 1
}

# True when the recorded PID is alive AND is still the process that took the
# lock. A lock written before start times were recorded trusts the PID alone.
lock_holder_live() {
    local pid="$1" recorded current
    [ -n "${pid}" ] && /bin/kill -0 "${pid}" 2>/dev/null || return 1
    recorded="$(/bin/cat "${LOCK_DIR}/started" 2>/dev/null)"
    [ -z "${recorded}" ] && return 0
    current="$(start_time_of "${pid}")"
    [ "${recorded}" = "${current}" ] && return 0
    # A lock taken by the previous script recorded the writer's local format.
    # Respect it rather than remove a live holder's lock on the first run.
    current="$(/bin/ps -o lstart= -p "${pid}" 2>/dev/null)"
    [ "${recorded}" = "${current}" ]
}

if ! acquire_lock; then
    HOLDER_PID="$(/bin/cat "${LOCK_DIR}/pid" 2>/dev/null)"
    if lock_holder_live "${HOLDER_PID}"; then
        finish failed_lock "pipeline lock held by live PID ${HOLDER_PID} (${LOCK_DIR}); not deploying" 0
    fi
    echo "removing stale lock ${LOCK_DIR} (recorded PID '${HOLDER_PID}' not running, or reused by another process)"
    /bin/rm -rf "${LOCK_DIR}"
    if ! acquire_lock; then
        finish failed_lock "lost lock race after stale removal (${LOCK_DIR})" 0
    fi
fi
LOCK_HELD=1

# --- 4. repo-prod preconditions ----------------------------------------------
if [ ! -d "${PROD_REPO}/.git" ]; then
    finish failed_precondition "not a git checkout: ${PROD_REPO}" 1
fi

PROD_STATUS="$("${GIT}" -C "${PROD_REPO}" status --porcelain 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_precondition "git status failed in ${PROD_REPO}: ${PROD_STATUS}" 1
fi
if [ -n "${PROD_STATUS}" ]; then
    # A dirty production tree means somebody edited the checkout automation runs
    # from. Deploying would either fail or silently carry those edits forward.
    finish blocked_dirty_tree "${PROD_REPO} has local modifications; refusing to deploy:
${PROD_STATUS}" 1
fi

PROD_BRANCH="$("${GIT}" -C "${PROD_REPO}" rev-parse --abbrev-ref HEAD 2>&1)"
if [ "${PROD_BRANCH}" != "${BRANCH}" ]; then
    finish failed_precondition "${PROD_REPO} is on '${PROD_BRANCH}', expected '${BRANCH}'" 1
fi

FROM_SHA="$("${GIT}" -C "${PROD_REPO}" rev-parse HEAD 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_precondition "could not read HEAD of ${PROD_REPO}: ${FROM_SHA}" 1
fi

FETCH_OUT="$("${GIT}" -C "${PROD_REPO}" fetch --quiet "${REMOTE}" "${BRANCH}" 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_precondition "git fetch ${REMOTE} ${BRANCH} failed: ${FETCH_OUT}" 1
fi

TO_SHA="$("${GIT}" -C "${PROD_REPO}" rev-parse "${DEPLOY_REF}" 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_precondition "could not resolve ${DEPLOY_REF}: ${TO_SHA}" 1
fi

if [ "${FROM_SHA}" = "${TO_SHA}" ]; then
    finish noop_already_current "${PROD_REPO} is already at ${TO_SHA}" 0
fi

if ! "${GIT}" -C "${PROD_REPO}" merge-base --is-ancestor "${FROM_SHA}" "${TO_SHA}"; then
    finish blocked_not_fast_forward "${TO_SHA} is not a descendant of deployed ${FROM_SHA}; resolve by hand" 1
fi

BEHIND="$("${GIT}" -C "${PROD_REPO}" rev-list --count "${FROM_SHA}..${TO_SHA}" 2>/dev/null)"
echo "candidate ${TO_SHA} is ${BEHIND:-?} commit(s) ahead of deployed ${FROM_SHA}"

# --- 5. staging clone --------------------------------------------------------
# Tests run HERE, never in repo-prod: gating in the target would mean deploying
# before testing. The stage is persistent so its .venv and uv cache stay warm;
# it is reset hard each run so a previous failure cannot leave state behind.
PROD_ORIGIN="$("${GIT}" -C "${PROD_REPO}" remote get-url "${REMOTE}" 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_precondition "could not read ${REMOTE} URL from ${PROD_REPO}: ${PROD_ORIGIN}" 1
fi

if [ ! -d "${STAGE_DIR}/.git" ]; then
    echo "creating staging clone at ${STAGE_DIR}"
    /bin/mkdir -p "$(/usr/bin/dirname "${STAGE_DIR}")"
    CLONE_OUT="$("${GIT}" clone --quiet "${PROD_ORIGIN}" "${STAGE_DIR}" 2>&1)"
    if [ $? -ne 0 ]; then
        finish failed_stage "could not clone ${PROD_ORIGIN} into ${STAGE_DIR}: ${CLONE_OUT}" 1
    fi
fi

# Identity, not liveness: a stage pointing at a different repository would run a
# green suite for code that is not the code being deployed.
STAGE_ORIGIN="$("${GIT}" -C "${STAGE_DIR}" remote get-url "${REMOTE}" 2>&1)"
if [ "${STAGE_ORIGIN}" != "${PROD_ORIGIN}" ]; then
    finish failed_stage "staging clone ${STAGE_DIR} points at '${STAGE_ORIGIN}', production at '${PROD_ORIGIN}'" 1
fi

STAGE_FETCH="$("${GIT}" -C "${STAGE_DIR}" fetch --quiet "${REMOTE}" "${BRANCH}" 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_stage "git fetch in ${STAGE_DIR} failed: ${STAGE_FETCH}" 1
fi

RESET_OUT="$("${GIT}" -C "${STAGE_DIR}" reset --hard --quiet "${TO_SHA}" 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_stage "could not reset ${STAGE_DIR} to ${TO_SHA}: ${RESET_OUT}" 1
fi
# -e .venv keeps the warm environment; everything else from a prior run goes.
"${GIT}" -C "${STAGE_DIR}" clean -qfdx -e .venv

STAGE_SHA="$("${GIT}" -C "${STAGE_DIR}" rev-parse HEAD 2>&1)"
if [ "${STAGE_SHA}" != "${TO_SHA}" ]; then
    finish failed_stage "staging clone is at ${STAGE_SHA}, expected candidate ${TO_SHA}" 1
fi

# --- 6. the gate: full test suite --------------------------------------------
# The dashboard extra is required. Without it the entire API test module SKIPS,
# which would report a green suite that never exercised the dashboard.
echo "running gate suite in ${STAGE_DIR} at ${TO_SHA}"
GATE_STARTED="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
( cd "${STAGE_DIR}" && "${UV}" run --locked --extra dashboard pytest -q ) > "${TEST_LOG}" 2>&1
TEST_RC=$?
TESTS_SUMMARY="$(/usr/bin/grep -aE '^[0-9]+ (passed|failed)|passed|failed|error' "${TEST_LOG}" | /usr/bin/tail -1)"
echo "gate started ${GATE_STARTED}, finished $(/bin/date -u +%Y-%m-%dT%H:%M:%SZ), pytest exit ${TEST_RC}"
echo "gate summary: ${TESTS_SUMMARY:-<no summary line>}"

if [ ${TEST_RC} -ne 0 ]; then
    finish blocked_tests_failed "pytest exit ${TEST_RC} at ${TO_SHA}; repo-prod stays at ${FROM_SHA}. Full log: ${TEST_LOG}" 1
fi
# Exit 0 is not sufficient: pytest also exits 0 when it collected nothing.
if ! /usr/bin/grep -qaE '[0-9]+ passed' "${TEST_LOG}"; then
    finish blocked_tests_failed "pytest exited 0 but reported no passing tests; refusing to treat that as a green gate. Full log: ${TEST_LOG}" 1
fi

# --- 7. schema gate ----------------------------------------------------------
# Ask the CANDIDATE code whether the live database is current. --dry-run opens it
# read-only and cannot migrate. The command exits 0 whether or not upgrades are
# pending, so the JSON is what decides; the exit code alone would pass silently.
UPGRADE_JSON="$( cd "${STAGE_DIR}" && "${UV}" run --locked selfimprove upgrade-state --dry-run 2>&1 )"
UPGRADE_RC=$?
if [ ${UPGRADE_RC} -ne 0 ]; then
    finish blocked_pending_migrations "upgrade-state --dry-run failed (exit ${UPGRADE_RC}) under candidate ${TO_SHA}: ${UPGRADE_JSON}" 1
fi

PENDING="$(printf '%s' "${UPGRADE_JSON}" | "${PYTHON3}" -c "
import json, sys
data = json.load(sys.stdin)
pending = data.get('pending')
if pending is None:
    raise SystemExit('upgrade-state JSON has no pending key')
print(','.join(pending))
")"
PENDING_RC=$?
if [ ${PENDING_RC} -ne 0 ]; then
    finish blocked_pending_migrations "could not read 'pending' from upgrade-state JSON: ${UPGRADE_JSON}" 1
fi
if [ -n "${PENDING}" ]; then
    finish blocked_pending_migrations "candidate ${TO_SHA} needs schema upgrades the live database has not had: ${PENDING}. Apply them explicitly with 'selfimprove upgrade-state --backup <new private dir>', then re-run this deploy." 1
fi

# --- 8. move repo-prod -------------------------------------------------------
# --ff-only against the already-fetched SHA. No network, no working-tree edit
# beyond the fast-forward itself.
MERGE_OUT="$("${GIT}" -C "${PROD_REPO}" merge --ff-only --quiet "${TO_SHA}" 2>&1)"
if [ $? -ne 0 ]; then
    finish failed_merge "fast-forward of ${PROD_REPO} to ${TO_SHA} failed: ${MERGE_OUT}" 1
fi

DEPLOYED_SHA="$("${GIT}" -C "${PROD_REPO}" rev-parse HEAD 2>&1)"
if [ "${DEPLOYED_SHA}" != "${TO_SHA}" ]; then
    finish failed_merge "after merge ${PROD_REPO} is at ${DEPLOYED_SHA}, expected ${TO_SHA}" 1
fi

POST_STATUS="$("${GIT}" -C "${PROD_REPO}" status --porcelain 2>&1)"
if [ -n "${POST_STATUS}" ]; then
    finish failed_merge "${PROD_REPO} is dirty after fast-forward: ${POST_STATUS}" 1
fi

# --- 9. dependency sync ------------------------------------------------------
# Pay the dependency cost now, at 01:00, rather than letting the 02:30 pipeline
# discover a broken resolve. A sync failure does NOT roll the checkout back: the
# code is already the tested code, and the nightly's own `uv run` would retry.
# It is reported as a non-fatal warning with its own log line.
# --locked: test and install exactly the committed uv.lock. Without it a stale
# lock is re-resolved here and rewritten in repo-prod after the clean check.
SYNC_OUT="$("${UV}" sync --locked --project "${PROD_REPO}" --extra dashboard 2>&1)"
SYNC_RC=$?
if [ ${SYNC_RC} -ne 0 ]; then
    echo "WARNING: uv sync in ${PROD_REPO} exited ${SYNC_RC} after a successful deploy:"
    echo "${SYNC_OUT}"
fi

finish deployed "${PROD_REPO} ${FROM_SHA} -> ${TO_SHA} (${BEHIND:-?} commits); gate: ${TESTS_SUMMARY}; uv sync exit ${SYNC_RC}" 0
