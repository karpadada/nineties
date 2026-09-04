#!/bin/sh
set -eu

tap="karpadada/nineties"
formula="$tap/nineties"
repository_url="https://github.com/karpadada/nineties.git"

if ! command -v brew >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Nineties requires Homebrew, but the `brew` command was not found.

Install Homebrew from:
  https://brew.sh/
EOF
  exit 127
fi

if ! brew tap | grep -Fxq "$tap"; then
  brew tap "$tap" "$repository_url"
elif ! brew list --versions "$formula" >/dev/null 2>&1; then
  # Refresh a tap left behind by an earlier failed installation.
  brew update
fi

if ! brew list --versions "$formula" >/dev/null 2>&1; then
  brew install --HEAD "$formula"
fi

exec "$(brew --prefix "$formula")/bin/nineties" "$@"
