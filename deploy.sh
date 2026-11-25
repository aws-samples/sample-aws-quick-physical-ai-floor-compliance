#!/usr/bin/env bash
# Wrapper: ./deploy.sh [options]   (see README section "Deploy")
set -euo pipefail
# shellcheck source=scripts/_venv.sh
source "$(dirname "$0")/scripts/_venv.sh"
run_floor deploy "$@"
