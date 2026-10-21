#!/usr/bin/env bash
# Publish a cleaned snapshot of this private repo to the public mirror, at a release tag.
#
#   bash scripts/publish_public.sh TAG [--dry-run] [--source URL] [--remote URL] [--terms FILE] [--workdir DIR]
#
# 1. Fresh clone of main from --source (default: this repo's origin) into a temp dir (or --workdir).
# 2. git filter-repo over the whole history:
#    - removes private-packs/, packs/fitness/, packs/nutrition/, packs/study/ and every path in the
#      clone's .publicignore;
#    - replaces every term from --terms (default private-packs/.privacy-terms) with [redacted] in file
#      contents and commit messages (whole words, the same rule as scripts/privacy_check.sh).
# 3. Checks the result: scripts/privacy_check.sh --history (every commit's message, paths and diff) and
#    that no removed path is left in any commit. Aborts on any match.
# 4. Tags HEAD as TAG, prints a summary, and (without --dry-run) asks for "yes" before pushing main
#    with --force-with-lease and the tag to the public remote. A tag that already exists there is
#    refused: the mirror is frozen at its tags.
#
# --dry-run stops after the summary and keeps the work dir; a failed run keeps it too, for inspection. Needs git-filter-repo
# (pip install git-filter-repo). Never pushes anywhere but --remote; the clone's origin is removed.
set -euo pipefail
export LC_ALL=C

usage() {
  sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
die() {
  echo "publish: $*" >&2
  exit 1
}

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)

tag=
dry_run=false
source_url=
public_remote=https://github.com/xy9iao/orbit-agent.git
terms_file=$repo_root/private-packs/.privacy-terms
workdir=
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) dry_run=true ;;
    --source) source_url=${2:?--source needs a value}; shift ;;
    --remote) public_remote=${2:?--remote needs a value}; shift ;;
    --terms) terms_file=${2:?--terms needs a value}; shift ;;
    --workdir) workdir=${2:?--workdir needs a value}; shift ;;
    -h | --help) usage ;;
    -*) die "unknown option $1" ;;
    *) [[ -z $tag ]] || die "only one TAG"; tag=$1 ;;
  esac
  shift
done
[[ -n $tag ]] || usage
git check-ref-format "refs/tags/$tag" || die "'$tag' is not a valid tag name"
[[ -f $terms_file ]] || die "no terms file at $terms_file: refusing to publish without the privacy terms"
terms_file=$(cd "$(dirname "$terms_file")" && pwd)/$(basename "$terms_file")
source_url=${source_url:-$(git -C "$repo_root" remote get-url origin)}

if git filter-repo --version >/dev/null 2>&1; then
  filter_repo=(git filter-repo)
elif command -v git-filter-repo >/dev/null 2>&1; then
  filter_repo=(git-filter-repo)
else
  die "git-filter-repo not found (pip install git-filter-repo)"
fi

if [[ -z $workdir ]]; then
  workdir=$(mktemp -d "${TMPDIR:-/tmp}/orbit-publish.XXXXXX")
elif [[ -e $workdir && -n $(ls -A "$workdir") ]]; then
  die "--workdir $workdir is not empty"
fi
mkdir -p "$workdir"
clone=$workdir/repo
finished=false
cleanup() { if $finished && ! $dry_run; then rm -rf "$workdir"; fi; }
trap cleanup EXIT

# ---- 1. fresh clone ---------------------------------------------------------------------------------
echo "publish: cloning main from $source_url"
git clone --quiet --no-local --single-branch --branch main "$source_url" "$clone"
cd "$clone"

# ---- 2. rewrite history -----------------------------------------------------------------------------
paths=$workdir/remove-paths.txt
printf '%s\n' private-packs/ packs/fitness/ packs/nutrition/ packs/study/ >"$paths"
if [[ -f .publicignore ]]; then
  tr -d '\r' <.publicignore | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' >>"$paths" || true
fi

terms=$workdir/terms.txt
tr -d '\r' <"$terms_file" | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' >"$terms" || true
[[ -s $terms ]] || die "$terms_file has no terms"
# Whole-word regexes: not preceded or followed by an ASCII word character, like privacy_check.sh.
# (A plain --replace-text literal would also hit a term inside a longer word, e.g. inside a variable name.)
expressions=$workdir/replacements.txt
while IFS= read -r term; do
  escaped=$(printf '%s' "$term" | sed 's/[][\.*^$+?(){}|/]/\\&/g')
  printf 'regex:(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])==>[redacted]\n' "$escaped"
done <"$terms" >"$expressions"

echo "publish: rewriting history (removing $(wc -l <"$paths" | tr -d ' ') path(s), redacting $(wc -l <"$terms" | tr -d ' ') term(s))"
"${filter_repo[@]}" --force --quiet \
  --invert-paths --paths-from-file "$paths" \
  --replace-text "$expressions" \
  --replace-message "$expressions"
git remote remove origin 2>/dev/null || true # filter-repo already drops it; never push back to private

# ---- 3. verify ------------------------------------------------------------------------------------------
echo "publish: checking the rewritten history"
check_ok=true
PRIVACY_TERMS_FILE=$terms_file bash "$script_dir/privacy_check.sh" --history || check_ok=false
# Plain paths are prefixes from the repo root (glob:/regex: entries are left to filter-repo).
anchored=$workdir/remove-paths.regex
grep -v -e '^glob:' -e '^regex:' "$paths" | sed -e 's/[][\.*^$+?(){}|]/\\&/g' -e 's/^/^/' >"$anchored"
leftover=$(git log --all --format= --name-only | sort -u | grep -E -f "$anchored" || true)
if [[ -n $leftover ]]; then
  echo "publish: removed paths still present in history:" >&2
  printf '  %s\n' $leftover >&2
  check_ok=false
fi
$check_ok || die "the rewritten history still contains private data; nothing was pushed (work dir: $workdir)"

git tag -a "$tag" -m "Public release $tag"

# ---- 4. summary, confirm, push -------------------------------------------------------------------------
echo
echo "== public snapshot $tag =="
echo "commits:          $(git rev-list --count HEAD)"
echo "files:            $(git ls-files | wc -l | tr -d ' ')"
echo "top level:        $(git ls-tree --name-only HEAD | tr '\n' ' ')"
echo "removed paths:    $(tr '\n' ' ' <"$paths")"
echo "remaining matches: 0 (privacy_check --history passed)"
echo "head:             $(git log -1 --format='%h %s')"
echo "work dir:         $clone"
echo

if $dry_run; then
  finished=true
  echo "publish: dry run, nothing pushed"
  exit 0
fi

git remote add public "$public_remote"
if [[ -n $(git ls-remote --tags public "refs/tags/$tag") ]]; then
  die "tag $tag already exists on $public_remote; the mirror is frozen at its tags, pick a new one"
fi
read -r -p "Push main and tag $tag to $public_remote? Type yes: " answer
[[ $answer == yes ]] || die "not confirmed; nothing pushed"

# Lease on what the public main really is now (empty expected value: it must not exist yet).
git fetch --quiet public main 2>/dev/null || true
expected=$(git rev-parse -q --verify refs/remotes/public/main || true)
git push --force-with-lease="main:$expected" public main
git push public "refs/tags/$tag"
finished=true
echo "publish: pushed main and $tag to $public_remote"
