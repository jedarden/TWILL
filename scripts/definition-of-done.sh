#!/bin/sh
# TWILL's definition of done: the plan §10.2 stop-ship gates that are
# mechanically runnable from a checkout, in one lane.
#
# The whole suite runs under the open-path audit hook (§8.3, §10.2): make
# test puts tests/ on PYTHONPATH so tests/sitecustomize.py installs the
# open()/os.open gate at interpreter startup and every spawned CLI verb
# self-installs it; a bare ungated unittest discover fails by design. pytest
# is the same suite under the runner §10.2 names — tests/conftest.py installs
# the same hook — and ruff is §10.2's lint gate.
#
# This script exists as a file, rather than as a README line, so the
# workspace declares its own verifier: NEEDLE's fallback gate runs it against
# a `git archive` extraction of committed state (no .git, no working tree),
# which is also why nothing here may depend on git or on untracked files.
#
# Usage: scripts/definition-of-done.sh [--fast]
#   --fast is accepted and identical: there is one lane.
# Prints one "command<TAB>exit" line per step; exits non-zero if any step
# failed. A missing optional tool is a loud SKIP, never a silent pass.

set -u

cd "$(dirname "$0")/.." || exit 41

case "${1:-}" in
  "" | --fast) ;;
  *)
    echo "definition-of-done: unknown flag '${1}' (expected no flag or --fast)" >&2
    exit 2
    ;;
esac

status=0
run() {
  "$@"
  rc=$?
  printf '%s\t%d\n' "$*" "$rc"
  if [ "$rc" -ne 0 ]; then
    status=1
  fi
  return 0
}

# The gated suite: the open-path stop-ship gate plus every unit test,
# including the mandatory secret-fixture and idempotency property tests.
run make test

# The same suite under the runner §10.2 names. Loud skip when absent:
# make test is the canonical stdlib-only entry and carries the lane alone.
if command -v pytest >/dev/null 2>&1; then
  run pytest -q
else
  printf 'pytest -q\tSKIP (pytest not on PATH)\n'
fi

# §10.2's lint gate, with the same loud-skip rule: a box without ruff on
# PATH still proves this tree's behaviour above, just not its style.
if command -v ruff >/dev/null 2>&1; then
  run ruff check .
else
  printf 'ruff check .\tSKIP (ruff not on PATH)\n'
fi

exit $status
