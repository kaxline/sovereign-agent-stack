#!/usr/bin/env bash
# CI's lint jobs, run locally in `make test` so `land` catches what they would.
# CI runs scripts/lint.sh in its own jobs, so this test is not tagged ci.
set -euo pipefail
exec bash "$(dirname "$0")/lint.sh"
