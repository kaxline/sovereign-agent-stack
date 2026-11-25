#!/bin/sh
# Apply one group of overlays.list, in list order: sh run-overlays.sh <group>.
# The numbered cont-init scripts call this. A patch that is not mounted is
# skipped; one that exits non-zero (an anchor moved in a Hermes bump) is
# logged as a warning and the next one still runs.
set -u
group="$1"
list=/bootstrap/overlays.list

run_patch() {
  script="$1"
  if [ -f "$script" ]; then
    python3 "$script" </dev/null || echo "warn: $script exited $?"
  fi
}

if [ ! -f "$list" ]; then
  echo "warn: $list is not mounted; group $group overlays not applied"
  exit 0
fi
# run_patch gives python3 </dev/null so a patch cannot read the rest of the list.
while read -r line_group name _; do
  if [ "$line_group" = "$group" ]; then
    run_patch "/bootstrap/$name"
  fi
done <"$list"
