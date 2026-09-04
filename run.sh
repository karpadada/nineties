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

installed=false
runs_web=false
case "${1:-web}" in
  web) runs_web=true ;;
esac

if ! brew tap | grep -Fxq "$tap"; then
  brew tap "$tap" "$repository_url"
elif brew list --versions "$formula" >/dev/null 2>&1; then
  installed=true
else
  # Refresh a tap left behind by an earlier failed installation.
  brew update
fi

if [ "$installed" = false ]; then
  brew install --HEAD "$formula"
elif [ "$runs_web" = true ]; then
  brew update
  brew upgrade --fetch-HEAD "$formula"
fi

executable="$(brew --prefix "$formula")/bin/nineties"
if [ ! -x "$executable" ]; then
  # Repair the 0.4.2 formula, which created a file named bin instead of this executable.
  brew update
  brew reinstall "$formula"
  executable="$(brew --prefix "$formula")/bin/nineties"
fi

exec "$executable" "$@"
