# The term-matching rule of scripts/privacy_check.sh (source it; needs LC_ALL=C).
#
# How a privacy term matches:
# - a term containing any CJK character (Chinese, Japanese kana, Korean, fullwidth forms) matches as a
#   plain substring: CJK text has no spaces, so 保密 must also match inside 保密词;
# - every other term matches as a whole word: not preceded or followed by an ASCII letter, digit or _,
#   so a term never matches inside an identifier such as PGPASSWORD.

# UTF-8 lead bytes of U+3000-U+D7FF (CJK punctuation, kana, CJK ideographs, Yi, Hangul), U+F900-U+FAFF
# (compatibility ideographs), U+FF00-U+FFFF (fullwidth forms) and U+20000-U+3FFFF (ideograph extensions).
PRIVACY_CJK_BYTES=$'[\xe3-\xed]|\xef[\xa4-\xab\xbc-\xbf]|\xf0[\xa0-\xbf]'

# privacy_read_terms FILE: the terms, one per line, without CR, comments and blank lines.
privacy_read_terms() {
  tr -d '\r' <"$1" | grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' || true
}

# privacy_is_cjk TERM: exit 0 if TERM contains a CJK character.
privacy_is_cjk() {
  printf '%s' "$1" | grep -q -E "$PRIVACY_CJK_BYTES"
}

# privacy_split_terms TERMS WORDS SUBSTRINGS: write whole-word terms to WORDS, CJK terms to SUBSTRINGS.
privacy_split_terms() {
  : >"$2"
  : >"$3"
  local term
  while IFS= read -r term; do
    if privacy_is_cjk "$term"; then
      printf '%s\n' "$term" >>"$3"
    else
      printf '%s\n' "$term" >>"$2"
    fi
  done <"$1"
}
