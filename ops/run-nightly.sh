#!/bin/bash
# Nightly self-improve run, invoked by launchd (com.tonygwu.self-improve).
#
# launchd runs jobs with a minimal PATH that excludes ~/.local/bin and
# /opt/homebrew/bin, so EVERY command in this script is an absolute path.
# All output goes to stdout/stderr; the plist redirects both into
# the configured state directory's logs/ subdirectory.
#
# LOCKING (verified 2026-08-14): macOS ships no flock(1), and /usr/bin/shlock
# is broken for stale locks — it detects the dead PID but a "lock time
# changed" guard always refuses to remove or take over the lock, so one
# crashed run would wedge every future night. This script therefore uses a
# mkdir-based lock with stale-PID detection: mkdir(2) is atomic, the holder
# PID is recorded inside the lock directory, and a lock whose recorded PID is
# no longer alive is removed and re-acquired. Known small race: between mkdir
# and writing the pid file another checker could see an empty lock and treat
# it as stale; with a single 02:30 launchd scheduler plus rare manual runs,
# contention at that microsecond window is not a realistic failure mode.

# -u and pipefail, deliberately NOT -e. Every failure below is handled
# explicitly: `PICK_JSON="$(...)"; PICK_RC=$?` reads the return code and prints
# a diagnostic naming the cause. Under `set -e` the assignment would abort the
# script first, so the checks would be dead code and launchd would record a
# bare non-zero exit with nothing explaining it. Do not add -e without
# rewriting those checks.
set -u -o pipefail

# Absolute defaults are what launchd needs; the ${VAR:-default} form exists so
# the test suite can point them at fakes and exercise this script for real
# instead of asserting on its source text. launchd sets none of these, so the
# defaults are what actually runs in production.
STATE_DIR="${SI_STATE_DIR:-${HOME}/.self-improve}"
LOCK_DIR="${SI_LOCK_DIR:-${STATE_DIR}/run.lock}"
QUOTAPICK="${SI_QUOTAPICK:-${HOME}/.local/bin/quotapick}"
PYTHON3="${SI_PYTHON3:-/usr/bin/python3}"
# Same ${VAR:-default} contract as the four above, and for the same reason:
# the test suite points these at fakes and EXECUTES this script, rather than
# asserting on its source text. launchd sets neither, so the defaults run.
PMSET="${SI_PMSET:-/usr/bin/pmset}"
CAFFEINATE_BIN="${SI_CAFFEINATE:-/usr/bin/caffeinate}"
# The checkout this script runs the pipeline against. Derived from the
# script's OWN location (ops/run-nightly.sh -> repo root) so that whichever
# checkout launchd points at is the one that runs. The plist names the
# production checkout (repo-prod); this makes that the ONLY place the
# production path is spelled. Deriving the root avoids a stale literal when
# a checkout moves. Tests must exercise the default as well as SI_PROJECT.
_OPS_DIR="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${SI_PROJECT:-$(/usr/bin/dirname "${_OPS_DIR}")}"

# --- 0. PATH ------------------------------------------------------------------
# Spelling out absolute paths for the binaries THIS SCRIPT calls is necessary
# and not sufficient. The pipeline then spawns the agent CLIs, and both `claude`
# and `codex` are Node programs whose wrappers invoke `node` by bare name;
# `project_identity` shells out to `gh` the same way. None of those resolve
# under launchd's PATH.
#
# A missing interpreter prevents model calls from starting. An unavailable
# gh also makes project identity fall back to remote_url, whose identity can
# change when a repository is renamed.
#
# Prepend rather than replace: /usr/bin still has to resolve, and SI_PATH_EXTRA
# exists so the test suite can drive these lines without depending on this
# machine's layout. tests/test_ops.py runs THESE TWO LINES under a launchd-like
# env and reads the resulting PATH back with printenv.
#
# `export` is defensive only: PATH is already exported in any environment
# launchd provides, so the assignment alone would be enough. Sabotage confirms
# it — commenting out the export changes nothing observable. Do not read its
# presence as evidence that a test is watching it; the assignment, the
# prepend and the default's contents are what the tests pin.
PATH="${SI_PATH_EXTRA:-${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin}:${PATH}"
export PATH

# uv can be installed by its standalone installer or by either Homebrew prefix.
# Resolve after setting PATH and keep an absolute executable for launchd.
UV="${SI_UV:-$(command -v uv)}"
if [[ "${UV}" != /* || ! -x "${UV}" ]]; then
    echo "nightly setup failed: uv was not found as an absolute executable; install uv or set SI_UV to its absolute path"
    exit 1
fi

/bin/mkdir -p "${STATE_DIR}/logs"

# --- 1. single-instance lock -------------------------------------------------
acquire_lock() {
    if /bin/mkdir "${LOCK_DIR}" 2>/dev/null; then
        echo "$$" > "${LOCK_DIR}/pid"
        return 0
    fi
    return 1
}

if ! acquire_lock; then
    HOLDER_PID="$(/bin/cat "${LOCK_DIR}/pid" 2>/dev/null)"
    if [ -n "${HOLDER_PID}" ] && /bin/kill -0 "${HOLDER_PID}" 2>/dev/null; then
        echo "skip night: lock held by live PID ${HOLDER_PID} (${LOCK_DIR})"
        exit 0
    fi
    echo "removing stale lock ${LOCK_DIR} (recorded PID '${HOLDER_PID}' not running)"
    /bin/rm -rf "${LOCK_DIR}"
    if ! acquire_lock; then
        echo "skip night: lost lock race after stale removal (${LOCK_DIR})"
        exit 0
    fi
fi
# INT/TERM/HUP are converted to exits so the EXIT trap releases the lock; a
# SIGKILL leaves a stale lock, which the dead-PID check above reclaims.
trap '/bin/rm -rf "${LOCK_DIR}"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 2. quota preflight ------------------------------------------------------
# Only an explicit fits=false skips quietly; quotapick failing or emitting
# unparseable JSON is a broken preflight and exits non-zero (fail loud).
PICK_JSON="$(${QUOTAPICK} pick --model sonnet --json)"
PICK_RC=$?
if [ ${PICK_RC} -ne 0 ]; then
    echo "quota preflight failed: quotapick exit ${PICK_RC}"
    echo "${PICK_JSON}"
    exit 1
fi
# fits=false has TWO very different causes and they need opposite handling:
#
#   * genuinely tight quota  -> a reset will fix it. Skip quietly, exit 0.
#   * a DARK fleet (expired OAuth tokens) -> no reset will ever fix it; only
#     an interactive /login will. Exiting 0 there means launchd records
#     success while the miner silently does nothing, every night, until
#     someone inspects the logs.
#
# quotapick reports the second case as rows whose reason starts "unreadable:"
# (remaining is null, not 0.0 — unknown is not zero).
VERDICT="$(printf '%s' "${PICK_JSON}" | ${PYTHON3} -c "
import json, sys
d = json.load(sys.stdin)
dark = []
for key in (\"degraded\", \"excluded\"):
    for row in (d.get(key) or []):
        if not isinstance(row, dict):
            continue
        reason = str(row.get(\"reason\") or \"\")
        if reason.startswith(\"unreadable:\"):
            who = row.get(\"account\") or row.get(\"id\") or \"?\"
            dark.append(str(who) + \": \" + reason)
if d[\"decision\"][\"fits\"]:
    print(\"RUN\")
elif dark:
    print(\"DARK \" + \"; \".join(sorted(set(dark))))
else:
    print(\"SKIP\")
")"
VERDICT_RC=$?
if [ ${VERDICT_RC} -ne 0 ]; then
    echo "quota preflight failed: could not read decision.fits from quotapick JSON (python exit ${VERDICT_RC})"
    echo "${PICK_JSON}"
    exit 1
fi
case "${VERDICT}" in
    RUN) ;;
    SKIP)
        echo "skip night: quota tight"
        exit 0
        ;;
    DARK*)
        echo "quota preflight failed: fleet is DARK - ${VERDICT#DARK }"
        echo "Remaining quota is UNKNOWN, not zero, and no reset clears this."
        echo "Fix with an interactive login, then re-enable the nightly."
        exit 1
        ;;
    *)
        echo "quota preflight failed: unrecognized verdict ${VERDICT}"
        echo "${PICK_JSON}"
        exit 1
        ;;
esac

# --- 3. the run itself -------------------------------------------------------
# --review-only until the miner self-eval clears the plan's bar
# (>=4/5 labeled incidents rediscovered, precision >=0.7): proposals are
# mined, gated, and reported nightly but nothing auto-applies. Remove the
# flag only after explicitly enabling the intended automatic target classes.
# Hold the machine awake only on AC power. Sleep can extend wall time while
# the run still holds its lock, causing the next scheduled invocation to skip.
# On battery, permit sleep to avoid keeping the machine awake for a long run.
# The mine stage's cfg.max_run_wall_seconds deadline bounds further mining.
#
# An ARRAY, not a string: a quoted empty string would be passed to `uv` as a
# literal empty argv[1], and an unquoted one only works until someone sets
# IFS. `caffeinate -i <cmd>` holds the assertion for exactly the child's
# lifetime and exits with its status, so RUN_RC stays the pipeline's.
#
# And it is expanded as ${CAFFEINATE[@]+"${CAFFEINATE[@]}"}, not the obvious
# "${CAFFEINATE[@]}". /bin/bash on macOS is 3.2.57, where an EMPTY array
# expanded under `set -u` is an unbound-variable error, not an empty list.
# Caught by tests/test_nightly_preflight.py executing this script rather than
# reading it: the plain form aborted the run with
# `CAFFEINATE[@]: unbound variable` on exactly the battery nights this branch
# exists to serve, which is every night the machine is not plugged in.
CAFFEINATE=()
if "${PMSET}" -g batt 2>/dev/null | /usr/bin/grep -q "AC Power"; then
    CAFFEINATE=("${CAFFEINATE_BIN}" -i)
    echo "power: on AC — holding the machine awake for this run"
else
    echo "power: on battery — NOT holding the machine awake; the run may sleep,"
    echo "       and the mine stage's wall-clock deadline is what bounds it"
fi

${CAFFEINATE[@]+"${CAFFEINATE[@]}"} "${UV}" run --project "${PROJECT}" selfimprove run --review-only 2>&1
RUN_RC=$?

# --- 4. propagate the pipeline's exit code -----------------------------------
exit ${RUN_RC}
