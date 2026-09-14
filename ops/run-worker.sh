#!/bin/bash
# Run from the installed production checkout. Installation/upgrade is explicit;
# this service must not sync dependencies or change its checkout at startup.
set -u -o pipefail
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
exec "${_WORKER_UV}" run --no-sync --frozen selfimprove worker "$@"
