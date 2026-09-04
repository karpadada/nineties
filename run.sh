#!/bin/sh
set -eu

tap="karpadada/nineties"
formula="$tap/nineties"
repository_url="https://github.com/karpadada/nineties.git"

formula_is_installed() {
  brew list --formula --full-name 2>/dev/null | grep -Fxq "$formula"
}

repair_tap_if_diverged() {
  tap_repository="$(brew --repository "$tap" 2>/dev/null || true)"
  if [ -z "$tap_repository" ] || [ ! -d "$tap_repository/.git" ]; then
    return
  fi

  tap_head="$(git -C "$tap_repository" rev-parse HEAD 2>/dev/null || true)"
  tap_origin_head="$(git -C "$tap_repository" rev-parse origin/HEAD 2>/dev/null || true)"
  if [ -z "$tap_head" ] || [ -z "$tap_origin_head" ]; then
    return
  fi

  if [ "$tap_head" != "$tap_origin_head" ] \
    || ! git -C "$tap_repository" diff --quiet -- \
    || ! git -C "$tap_repository" diff --cached --quiet --; then
    echo "Repairing the divergent Nineties Homebrew tap..." >&2
    brew update-reset "$tap_repository"
  fi
}

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
elif formula_is_installed; then
  installed=true
else
  # Refresh a tap left behind by an earlier failed installation.
  brew update
fi

if [ "$installed" = false ]; then
  brew install --HEAD "$formula"
elif [ "$runs_web" = true ]; then
  brew update
  repair_tap_if_diverged
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
