#!/usr/bin/env bash
# Fail if any tracked file contains a private term (health details, exact numbers, anything personal).
#
# Terms are read from private-packs/.privacy-terms (git-ignored), one per line; blank lines and lines
# starting with # are skipped. Matching is literal, case-sensitive and whole-word: a term never matches
# inside a longer ASCII word, and anything other than ASCII letters, digits and _ (CJK characters,
# punctuation) counts as a word boundary. Override the file with PRIVACY_TERMS_FILE=path.
#
#   bash scripts/privacy_check.sh             # check the tracked files in the working tree
#   bash scripts/privacy_check.sh --staged    # check what is staged (the pre-commit hook uses this)
#   bash scripts/privacy_check.sh --history   # check every commit: messages, paths and diffs
#                                             # (git log -p --all; scripts/publish_public.sh uses this)
#
# Exit 0: no matches (or no terms file, with a note). Exit 1: matches, listed with line numbers.
set -euo pipefail
# Byte-wise matching, so "word character" means the same thing on every machine.
export LC_ALL=C

cd "$(git rev-parse --show-toplevel)"
terms_file=${PRIVACY_TERMS_FILE:-private-packs/.privacy-terms}
mode=${1:-tree}
case $mode in
  tree | --staged | --history) ;;
  *)
    echo "usage: $0 [--staged | --history]" >&2
    exit 2
    ;;
esac

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
n_terms=$(wc -l <"$patterns" | tr -d ' ')

# -F literal; -w whole words; -n line numbers; -I (git grep) skips binary files.
# git grep and grep exit 1 when nothing matches.
case $mode in
  tree) found=$(git grep -n -I -F -w -f "$patterns" -- . || true) ;;
  --staged) found=$(git grep --cached -n -I -F -w -f "$patterns" -- . || true) ;;
  --history) found=$(git log -p --all --format='commit %H%n%B' | grep -n -a -F -w -f "$patterns" || true) ;;
esac

if [[ -n $found ]]; then
  printf '%s\n' "$found"
  echo "privacy check: FAILED - $(printf '%s\n' "$found" | wc -l | tr -d ' ') line(s) above contain terms from $terms_file" >&2
  exit 1
fi
echo "privacy check: ok ($n_terms terms${mode:+, $mode})"
