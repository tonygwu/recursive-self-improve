#!/bin/bash
# Run from the installed production checkout. Installation/upgrade is explicit;
# this service must not sync dependencies or change its checkout at startup.
set -u -o pipefail
# Use this checkout's preinstalled environment, even from another active shell.
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV UV_PROJECT UV_WORKING_DIR \
    UV_PROJECT_ENVIRONMENT UV_ENV_FILE UV_CONFIG_FILE UV_ISOLATED
_JOBS_OPS_DIR="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")" && pwd)"
_JOBS_PROJECT="$(/usr/bin/dirname "${_JOBS_OPS_DIR}")"
PATH="${SI_PATH_EXTRA:-${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin}:${PATH}"
export PATH
_JOBS_UV="${SI_UV:-$(command -v uv)}"
if [[ "${_JOBS_UV}" != /* || ! -x "${_JOBS_UV}" ]]; then
    echo "model-worker setup failed: install uv or set SI_UV to its absolute executable path" >&2
    exit 1
fi
cd "${_JOBS_PROJECT}" || exit 1
if [[ -n "${SI_CONFIG:-}" ]]; then
    if [[ "${SI_CONFIG}" != /* || ! -f "${SI_CONFIG}" ]]; then
        echo "model-worker setup failed: SI_CONFIG must name an existing absolute config file" >&2
        exit 2
    fi
    exec "${_JOBS_UV}" run --no-sync --frozen selfimprove --config "${SI_CONFIG}" jobs "$@"
fi
if [[ -n "${SI_SERVICE_STATE_DB:-}" ]]; then
    echo "model-worker setup failed: installed service requires SI_CONFIG" >&2
    exit 2
fi
exec "${_JOBS_UV}" run --no-sync --frozen selfimprove jobs "$@"
