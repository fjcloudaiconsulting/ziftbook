#!/usr/bin/env bash
# Fails when a build or test log holds a warning (ZIF-153): a line with Next's "⚠", or a Node runtime warning
# "(node:<pid>) ...", anywhere on the line (stderr merged into stdout can land mid-line). Prints the lines found.
# Exit 0: clean; 1: warnings; 2: the log cannot be read or grep failed (the gate fails closed).
# Run: scripts/fail-on-warnings.sh <log>
set -uo pipefail

log=${1:?usage: fail-on-warnings.sh <log>}
[ -r "$log" ] || { echo "fail-on-warnings: cannot read $log" >&2; exit 2; }

# Known-benign lines, each with its reason. Extended regular expressions, matched anywhere on the line.
allow=(
  # Next on CI when .next/cache misses (a cold cache key): the build runs uncached, nothing is wrong.
  'No build cache found\. Please configure build caching'
)

found=$(grep -E '⚠|\(node:[0-9]+\) ' "$log")
case $? in
  0) ;;
  1) exit 0 ;;
  *) echo "fail-on-warnings: grep failed on $log" >&2; exit 2 ;;
esac

args=()
for pattern in "${allow[@]}"; do args+=(-e "$pattern"); done
left=$(printf '%s\n' "$found" | grep -vE "${args[@]}")
case $? in
  0) printf 'Warnings in %s (fix them, or allow a benign one in scripts/fail-on-warnings.sh with its reason):\n%s\n' "$log" "$left"; exit 1 ;;
  1) exit 0 ;;
  *) echo "fail-on-warnings: grep failed on $log" >&2; exit 2 ;;
esac
