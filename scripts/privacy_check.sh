#!/usr/bin/env bash
# Fail if any tracked file contains a private term (health details, exact numbers, anything personal).
#
# Terms are read from private-packs/.privacy-terms (git-ignored), one per line; blank lines and lines
# starting with # are skipped. Matching is literal and case-sensitive. A term with a CJK character
# matches anywhere (保密 inside 保密词); any other term matches as a whole word only, never inside a
# longer ASCII word (a term never matches inside an identifier such as PGPASSWORD). The rule lives in
# scripts/privacy_terms.sh. Override the file with PRIVACY_TERMS_FILE=path.
#
#   bash scripts/privacy_check.sh             # check the tracked files in the working tree
#   bash scripts/privacy_check.sh --staged    # check what is staged (the pre-commit hook uses this)
#   bash scripts/privacy_check.sh --history   # check every commit: messages, paths and diffs
#                                             # (git log -p --all)
#
# Exit 0: no matches (or no terms file, with a note). Exit 1: matches, listed with line numbers.
set -euo pipefail
# Byte-wise matching, so "word character" means the same thing on every machine.
export LC_ALL=C

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=privacy_terms.sh
. "$script_dir/privacy_terms.sh"

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

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
privacy_read_terms "$terms_file" >"$tmp/terms"
if [[ ! -s $tmp/terms ]]; then
  echo "privacy check: $terms_file has no terms"
  exit 0
fi
n_terms=$(wc -l <"$tmp/terms" | tr -d ' ')
privacy_split_terms "$tmp/terms" "$tmp/words" "$tmp/substrings"

# search [-w] PATTERNS: matching lines, as "file:line:text" (tree, --staged) or "line:text" (--history).
# -F literal; -n line numbers; -I (git grep) skips binary files; -a reads the log as text.
# git grep and grep exit 1 when nothing matches.
search() {
  local word=() patterns=$1
  if [[ $1 == -w ]]; then word=(-w); patterns=$2; fi
  [[ -s $patterns ]] || return 0
  case $mode in
    tree) git grep -n -I -F "${word[@]}" -f "$patterns" -- . || true ;;
    --staged) git grep --cached -n -I -F "${word[@]}" -f "$patterns" -- . || true ;;
    --history) grep -n -a -F "${word[@]}" -f "$patterns" "$tmp/log" || true ;;
  esac
}
[[ $mode != --history ]] || git log -p --all --format='commit %H%n%B' >"$tmp/log"
# A line can match both lists; list it once, in file and line order.
order=(-k1,1 -k2,2n)
[[ $mode != --history ]] || order=(-k1,1n)
found=$({ search -w "$tmp/words"; search "$tmp/substrings"; } | sort -t: -u "${order[@]}")

if [[ -n $found ]]; then
  printf '%s\n' "$found"
  echo "privacy check: FAILED - $(printf '%s\n' "$found" | wc -l | tr -d ' ') line(s) above contain terms from $terms_file" >&2
  exit 1
fi
echo "privacy check: ok ($n_terms terms${mode:+, $mode})"
