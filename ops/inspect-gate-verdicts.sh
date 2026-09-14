#!/bin/bash
# Inspect one run and its trial artifacts without creating or migrating state.
# Usage: ops/inspect-gate-verdicts.sh [run-id] (default: newest run).
set -eu -o pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/ops/inspect_gate_verdicts.py" "$@"
