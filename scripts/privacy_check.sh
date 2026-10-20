#!/usr/bin/env bash
# Fail if any tracked file contains a private term (health details, exact numbers, anything personal).
#
# Terms are read from private-packs/.privacy-terms (git-ignored), one per line; blank lines and lines
# starting with # are skipped. Matching is literal, case-sensitive and whole-word (a term never matches
# inside a longer word; CJK characters and punctuation count as word boundaries). Override the file with
# PRIVACY_TERMS_FILE=path.
#
#   bash scripts/privacy_check.sh            # check the tracked files in the working tree
#   bash scripts/privacy_check.sh --staged   # check what is staged (the pre-commit hook uses this)
#
# Exit 0: no matches (or no terms file, with a note). Exit 1: matches, listed as file:line.
# Only the current files are checked, not git history.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
terms_file=${PRIVACY_TERMS_FILE:-private-packs/.privacy-terms}
scope=()
if [[ ${1:-} == --staged ]]; then
  scope=(--cached)
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--staged]" >&2
  exit 2
fi

if [[ ! -f $terms_file ]]; then
  echo "privacy check: no $terms_file, nothing to check"
  exit 0
fi

patterns=$(mktemp)
trap 'rm -f "$patterns"' EXIT
# Strip CR (a file saved on Windows), comments and blank lines.
tr -d '\r' <"$terms_file" | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' >"$patterns" || true
if [[ ! -s $patterns ]]; then
  echo "privacy check: $terms_file has no terms"
  exit 0
fi

# -I skips binary files; -F literal; -w whole words; -n line numbers. git grep exits 1 on no match.
if git grep "${scope[@]}" -n -I -F -w -f "$patterns" -- .; then
  echo "privacy check: FAILED - the lines above contain terms from $terms_file" >&2
  exit 1
fi
echo "privacy check: ok ($(wc -l <"$patterns" | tr -d ' ') terms)"
