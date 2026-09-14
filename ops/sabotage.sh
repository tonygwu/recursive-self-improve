#!/usr/bin/env bash
# Prove a test is load-bearing: break the code, watch the test go red, put it
# back, and prove the tree really is back.
#
# A restore within the same mtime tick can leave cached Python bytecode valid
# when the file size also stays unchanged. Purge bytecode after each edit and
# restore so each test executes the intended source bytes.
#
#   ops/sabotage.sh <file> <old-string> <new-string> <pytest -k expression>
#
# Exits 0 only if the test was GREEN before, RED under the sabotage, and GREEN
# again after the restore. Anything else is a finding, and it says which.
set -euo pipefail

if [ $# -ne 4 ]; then
    sed -n '2,/^set /{ /^set /q; p; }' "$0" >&2
    exit 2
fi
FILE="$1"; OLD="$2"; NEW="$3"; K="$4"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 2; }

UV="${UV:-/opt/homebrew/bin/uv}"
BACKUP="$(mktemp)"; cp "$FILE" "$BACKUP"

# Belt and braces: restore from the backup AND drop every cached bytecode file,
# whatever happens from here.
cleanup() {
    cp "$BACKUP" "$FILE"
    rm -f "$BACKUP"
    find src tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
}
trap cleanup EXIT

# pytest exits 5 when NO test matched -k. That is a different finding from a
# failing test, and reporting it as "already red" would send you looking for a
# bug in code that no test is even naming.
run_k() {
    "$UV" run pytest -q -k "$K" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -eq 5 ]; then
        echo "NO TEST MATCHED -k '$K'. Nothing was run, so nothing is proven." >&2
        exit 1
    fi
    return $rc
}

purge() { find src tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true; }

# How many tests -k actually selected. A "did not bite" with a large or
# surprising number here can indicate a wrong -k expression. Selecting tests
# that do not exercise the changed line cannot establish whether it is guarded.
count_k() {
    "$UV" run pytest -q -k "$K" --collect-only 2>/dev/null | grep -c '::' || true
}

purge
if ! run_k; then
    echo "BEFORE: the test is already RED. Nothing is proven by breaking more." >&2
    exit 1
fi

python3 - "$FILE" "$OLD" "$NEW" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path, encoding="utf-8").read()
n = s.count(old)
if n != 1:
    # Not "if n == 0": TWO matches means the sabotage lands somewhere you did
    # not read, and a passing result would be meaningless.
    sys.exit(f"anchor appears {n} times, need exactly 1")
open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
PY

purge
if run_k; then
    N="$(count_k)"
    echo "SABOTAGE DID NOT BITE: every test matching -k '$K' still passes with" >&2
    echo "  the change applied." >&2
    echo "  file:  $FILE" >&2
    echo "  -k selected $N test(s)." >&2
    echo "  This means ONE of two things, and the tool cannot tell them apart:" >&2
    echo "    (a) no test covers this line -- write one; or" >&2
    echo "    (b) -k did not select the test that does -- fix the expression." >&2
    echo "  Check (b) FIRST. A -k matching the wrong tests reports a guarded" >&2
    echo "  line as unguarded, which is the more expensive mistake: it invites" >&2
    echo "  you to write a test that already exists, or to distrust a guard" >&2
    echo "  that works." >&2
    exit 1
fi

cleanup; trap - EXIT
purge
if ! run_k; then
    echo "RESTORE FAILED: '$K' is still red with the original file back." >&2
    exit 1
fi
echo "OK  sabotage bit, restore verified: $K"
