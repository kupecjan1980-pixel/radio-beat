#!/usr/bin/env bash
# One-command install / update (no git needed):
#   curl -fsSL https://raw.githubusercontent.com/kupecjan1980-pixel/radio-beat/master/get.sh | bash
set -euo pipefail
REPO=kupecjan1980-pixel/radio-beat
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# The newest commit, so the app knows which version it has (for its update check).
# (The app's updater passes the commit it saw as RADIO_BEAT_SHA.)
SHA=${RADIO_BEAT_SHA:-$(curl -fsSL "https://api.github.com/repos/$REPO/commits/master" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')}
curl -fsSL "https://github.com/$REPO/archive/$SHA.tar.gz" | tar -xz -C "$tmp"
cd "$tmp/radio-beat-$SHA"
RADIO_BEAT_VERSION=$SHA ./install.sh "$@"
