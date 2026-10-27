#!/usr/bin/env bash
#
# TEMPORARY DIAGNOSTIC (2026-07-25). Delete once the question below is answered.
#
# WHAT IT IS FOR
# --------------
# `containment_negative_controls.sh` aborts on the GitHub runner, and its own
# diagnostics say why: at that point in the job,
# `wrapper/tests/_egress_guard/sitecustomize.py` is not on disk. The abort is
# correct behaviour -- without the guard the controls cannot tell namespace
# denial from guard denial -- so the defect is the missing file, not the abort.
#
# WHAT IS ALREADY RULED OUT, so nobody re-runs these:
#   * The file IS in the commit the job checked out. Verified against the
#     GitHub trees API at 60a50fb: `100644 blob 5867
#     wrapper/tests/_egress_guard/sitecustomize.py`, one entry in that tree,
#     tree not truncated. Regular file, not a symlink, not a gitlink.
#   * Nothing in wrapper/ names the file except conftest.py (PYTHONPATH),
#     the runner, and the controls -- and none of them delete it.
#   * The whole CI sequence -- self-test, the contained suite under
#     `unshare -n` + `setpriv` drop, and the `--collect-only` drop proof --
#     was reproduced against a clean clone of this branch in WSL, with the
#     server package installed so the 13 cross-side parity checks really ran
#     (4190 passed / 122 skipped there vs 4193 / 119 on the runner). The file
#     survived every step and `git status` came back clean. The negative
#     controls then PASSED, 5/5. So the loss is something the runner does and
#     a local run does not, and guessing has run out of road.
#
# So this prints the state of that one path at every step boundary, and the run
# itself says which step loses it. It never fails the job -- an observation, not
# a gate -- because a diagnostic that can fail the run it is diagnosing just
# hides the next question behind its own.
#
# Deliberately NOT run under sudo: it must not leave root-owned git state
# behind for the steps that follow.
set -u

LABEL="${1:-unlabelled}"
GUARD_DIR="tests/_egress_guard"

echo "=== guard-file probe: ${LABEL} ==="
echo "cwd  : $PWD"
echo "whoami: $(id -un 2>/dev/null) (uid $(id -u 2>/dev/null))"

echo "--- ${GUARD_DIR}/ ---"
ls -la "$GUARD_DIR/" 2>&1 || echo "  (the directory itself is absent)"

echo "--- the file ---"
stat -c '  %n  type=%F mode=%a owner=%U:%G size=%s mtime=%y' \
    "$GUARD_DIR/sitecustomize.py" 2>&1 \
    || echo "  sitecustomize.py: ABSENT"

# `git status` distinguishes the two explanations that matter: a file the
# working tree lost reads as ` D `, a file checkout never wrote reads the same
# way -- but `git ls-files` then says whether the index still carries it, and
# `check-ignore` says whether anything would have skipped it.
echo "--- git ---"
git status --porcelain -- "$GUARD_DIR/" 2>&1 || true
git ls-files -s -- "$GUARD_DIR/" 2>&1 || true
git check-ignore -v -- "$GUARD_DIR/sitecustomize.py" 2>&1 || echo "  (not ignored)"

# Catches a move/rename rather than a delete, which `ls` alone would report
# identically.
echo "--- anything named sitecustomize under wrapper/ ---"
find . -name 'sitecustomize*' -not -path './.git/*' 2>/dev/null || true

echo "=== end probe: ${LABEL} ==="
exit 0
