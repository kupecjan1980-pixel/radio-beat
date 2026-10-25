#!/usr/bin/env bash
# One-command install / update (no git needed):
#   curl -fsSL https://raw.githubusercontent.com/kupecjan1980-pixel/radio-beat/master/get.sh | bash
set -euo pipefail
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL https://github.com/kupecjan1980-pixel/radio-beat/archive/refs/heads/master.tar.gz | tar -xz -C "$tmp"
cd "$tmp/radio-beat-master"
./install.sh "$@"
