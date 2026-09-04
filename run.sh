#!/bin/sh
set -eu

tap="karpadada/nineties"
formula="$tap/nineties"
repository_url="https://github.com/karpadada/nineties.git"

formula_is_installed() {
  brew list --formula --full-name </dev/null 2>/dev/null | grep -Fxq "$formula"
}

repair_tap_if_diverged() {
  tap_repository="$(brew --repository "$tap" </dev/null 2>/dev/null || true)"
  if [ -z "$tap_repository" ] || [ ! -d "$tap_repository/.git" ]; then
    return
  fi

  tap_head="$(git -C "$tap_repository" rev-parse HEAD </dev/null 2>/dev/null || true)"
  tap_origin_head="$(git -C "$tap_repository" rev-parse origin/HEAD </dev/null 2>/dev/null || true)"
  if [ -z "$tap_head" ] || [ -z "$tap_origin_head" ]; then
    return
  fi

  if [ "$tap_head" != "$tap_origin_head" ] \
    || ! git -C "$tap_repository" diff --quiet -- </dev/null \
    || ! git -C "$tap_repository" diff --cached --quiet -- </dev/null; then
    echo "Repairing the divergent Nineties Homebrew tap..." >&2
    brew update-reset "$tap_repository" </dev/null
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

if ! brew tap </dev/null | grep -Fxq "$tap"; then
  brew tap "$tap" "$repository_url" </dev/null
elif formula_is_installed; then
  installed=true
else
  # Refresh a tap left behind by an earlier failed installation.
  brew update </dev/null
fi

if [ "$installed" = false ]; then
  brew install --HEAD "$formula" </dev/null
elif [ "$runs_web" = true ]; then
  brew update </dev/null
  repair_tap_if_diverged
  brew upgrade --fetch-HEAD "$formula" </dev/null
fi

executable="$(brew --prefix "$formula" </dev/null)/bin/nineties"
if [ ! -x "$executable" ]; then
  # Repair the 0.4.2 formula, which created a file named bin instead of this executable.
  brew update </dev/null
  brew reinstall "$formula" </dev/null
  executable="$(brew --prefix "$formula" </dev/null)/bin/nineties"
fi

exec "$executable" "$@"
