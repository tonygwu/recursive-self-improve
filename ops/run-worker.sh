#!/bin/bash
# Run from the installed production checkout. Installation/upgrade is explicit;
# this service must not sync dependencies or change its checkout at startup.
set -u -o pipefail
# Use this checkout's preinstalled environment, even from another active shell.
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV UV_PROJECT UV_WORKING_DIR \
    UV_PROJECT_ENVIRONMENT UV_ENV_FILE UV_CONFIG_FILE UV_ISOLATED
_WORKER_OPS_DIR="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")" && pwd)"
_WORKER_PROJECT="$(/usr/bin/dirname "${_WORKER_OPS_DIR}")"
PATH="${SI_PATH_EXTRA:-${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin}:${PATH}"
export PATH
_WORKER_UV="${SI_UV:-$(command -v uv)}"
if [[ "${_WORKER_UV}" != /* || ! -x "${_WORKER_UV}" ]]; then
    echo "worker setup failed: install uv or set SI_UV to its absolute executable path" >&2
    exit 1
fi
cd "${_WORKER_PROJECT}" || exit 1
if [[ -n "${SI_CONFIG:-}" ]]; then
    if [[ "${SI_CONFIG}" != /* || ! -f "${SI_CONFIG}" ]]; then
        echo "worker setup failed: SI_CONFIG must name an existing absolute config file" >&2
        exit 2
    fi
    exec "${_WORKER_UV}" run --no-sync --frozen selfimprove --config "${SI_CONFIG}" worker "$@"
fi
if [[ -n "${SI_SERVICE_STATE_DB:-}" ]]; then
    echo "worker setup failed: installed service requires SI_CONFIG" >&2
    exit 2
fi
exec "${_WORKER_UV}" run --no-sync --frozen selfimprove worker "$@"
