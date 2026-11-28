#!/usr/bin/env bash
# One-command install / update (no git needed):
#   curl -fsSL https://raw.githubusercontent.com/kupecjan1980-pixel/radio-beat/master/get.sh | bash
set -euo pipefail
REPO=kupecjan1980-pixel/radio-beat
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# The newest commit, so the app knows which version it has (for its update check).
# (The app's updater passes the commit it saw as RADIO_BEAT_SHA.)
# (git's own address, not the API: that one allows 60 requests an hour for the whole home network)
SHA=${RADIO_BEAT_SHA:-$(curl -fsSL -A git/2.0 "https://github.com/$REPO.git/info/refs?service=git-upload-pack" | grep -ao '[0-9a-f]\{40\} refs/heads/master$' | cut -c1-40)}
curl -fsSL "https://github.com/$REPO/archive/$SHA.tar.gz" | tar -xz -C "$tmp"
cd "$tmp/radio-beat-$SHA"
RADIO_BEAT_VERSION=$SHA ./install.sh "$@"
