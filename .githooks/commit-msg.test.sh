#!/bin/sh
# Self-check for .githooks/commit-msg. Run: make test-hooks
hook="$(dirname "$0")/commit-msg"
fail=0
check() { # expected(0|1) message
  f=$(mktemp); printf '%s\n' "$2" > "$f"
  "$hook" "$f" >/dev/null 2>&1; got=$?; rm -f "$f"
  [ "$got" -ne 0 ] && got=1
  if [ "$got" -ne "$1" ]; then echo "FAIL (want exit $1): $2"; fail=1; fi
}
check 0 "feat: add booking page"
check 0 "fix(api): reject overlapping bookings"
check 0 "feat(web)!: drop legacy route"
check 0 "chore: bump deps

Body text is not checked."
check 0 "Merge branch 'main' into feat/x"
check 0 'Revert "feat: add booking page"'
check 0 "fixup! feat: add booking page"
check 0 "squash! feat: add booking page"
check 1 "added booking page"
check 1 "feat add booking page"
check 1 "feature: add booking page"
check 1 "feat:"
check 1 "Feat: add booking page"
[ "$fail" -eq 0 ] && echo "commit-msg hook: all checks passed"
exit "$fail"
