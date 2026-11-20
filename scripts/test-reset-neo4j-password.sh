#!/usr/bin/env bash
# scripts/reset-neo4j-password.sh's cypher_string, which quotes the user name
# and password as Cypher string literals, under every bash it may run on.
# macOS's /bin/bash 3.2 and bash 5 read backslashes in ${v//pat/rep}
# differently; the script must give the same literal under both. The rest of
# the script needs the Neo4j image and a volume with another password, so it
# has no automated test.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fn="${tmp}/cypher_string.sh"
awk '/^cypher_string\(\) \{/,/^}/' "${ROOT}/scripts/reset-neo4j-password.sh" > "$fn"
[ -s "$fn" ] || fail "cypher_string not found in reset-neo4j-password.sh"

shells="bash"
[ -x /bin/bash ] && shells="${shells} /bin/bash"
# value|literal: ' and \ escaped, everything else as it is.
for sh in $shells; do
  while IFS='|' read -r value want; do
    # shellcheck disable=SC2016  # $1 and $2 expand in the inner shell
    got="$("$sh" -c '. "$1"; cypher_string "$2"' cs "$fn" "$value")"
    [ "$got" = "$want" ] || fail "${sh}: cypher_string [${value}]: want [${want}], got [${got}]"
  done <<'EOF'
plain|'plain'
a'b|'a\'b'
a\b|'a\\b'
a'b\c"d&e|'a\'b\\c"d&e'
\'|'\\\''
EOF
done
echo "ok reset-neo4j-password cypher_string ($shells)"
