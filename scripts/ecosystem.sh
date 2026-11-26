#!/usr/bin/env bash
# Clone the four sibling projects at the commits Themis was last tested against, install them in their own
# virtualenv, and run the ecosystem tests against them for real.
#
#   ./scripts/ecosystem.sh            # uses ./.siblings
#   ./scripts/ecosystem.sh /some/dir
#
# To test against newer commits, change the pins in scripts/pins.sh and run it again.
set -euo pipefail

dir="${1:-.siblings}"
source "$(dirname "$0")/pins.sh"
clone_pinned "$dir" "${!pins[@]}"

[ -x "$dir/venv/bin/python" ] || python3 -m venv "$dir/venv"
"$dir/venv/bin/pip" install --quiet \
  -e "$dir/agent-harness-sdk[server]" -e "$dir/aegis-war-room" -e "$dir/argus-sre-agent" -e "$dir/intentops"

abs="$(cd "$dir" && pwd)"
THEMIS_SIBLINGS="$abs" THEMIS_SIBLINGS_PYTHON="$abs/venv/bin/python" python -m pytest -m ecosystem -q
