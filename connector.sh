#!/usr/bin/env bash
# Wrapper: ./connector.sh   (see README section "Deploy")
set -euo pipefail
# shellcheck source=scripts/_venv.sh
source "$(dirname "$0")/scripts/_venv.sh"
run_floor connector "$@"
