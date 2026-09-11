#!/bin/sh

# Standalone diagnostics for installations that may be too broken to run the
# normal Nineties launcher. Keep this script POSIX-compatible so support can ask
# users to run it directly or pipe it to `sh` from GitHub.

set -u
umask 077

formula="${NINETIES_BREW_FORMULA:-karpadada/nineties/nineties}"
output_path=""
report_path=""
report_tmp=""
command_tmp=""

usage() {
  cat <<'EOF'
Usage: nineties doctor [--output PATH]
       doctor.sh [--output PATH]

Create a private diagnostic report without changing the Nineties installation.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output|-o)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "--output requires a file path." >&2
        exit 2
      fi
      output_path="$2"
      shift 2
      ;;
    --output=*)
      output_path=${1#--output=}
      if [ -z "$output_path" ]; then
        echo "--output requires a file path." >&2
        exit 2
      fi
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown doctor option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$output_path" ]; then
  output_path="nineties-doctor-$(date -u '+%Y%m%dT%H%M%SZ').txt"
fi
case "$output_path" in
  /*) report_path=$output_path ;;
  *) report_path="$(pwd -P)/$output_path" ;;
esac

if [ -e "$report_path" ]; then
  echo "Refusing to overwrite the existing report: $report_path" >&2
  exit 1
fi

report_parent=$(dirname -- "$report_path")
if [ ! -d "$report_parent" ]; then
  echo "The report directory does not exist: $report_parent" >&2
  exit 1
fi

report_tmp=$(mktemp "$report_parent/.nineties-doctor-report.XXXXXX") || exit 1
command_tmp=$(mktemp "$report_parent/.nineties-doctor-command.XXXXXX") || {
  rm -f -- "$report_tmp"
  exit 1
}

cleanup() {
  rm -f -- "$report_tmp" "$command_tmp"
}
trap cleanup EXIT HUP INT TERM

redact_stream() {
  awk -v home_path="${HOME:-}" '
    function redact_home(value, position, prefix) {
      if (home_path == "") {
        return value
      }
      prefix = ""
      while ((position = index(value, home_path)) != 0) {
        prefix = prefix substr(value, 1, position - 1) "~"
        value = substr(value, position + length(home_path))
      }
      return prefix value
    }
    { print redact_home($0) }
  '
}

heading() {
  printf '\n## %s\n' "$1" >>"$report_tmp"
}

record_value() {
  label=$1
  value=$2
  printf '%s: ' "$label" >>"$report_tmp"
  printf '%s\n' "$value" | redact_stream >>"$report_tmp"
}

capture() {
  label=$1
  shift
  printf '\n### %s\n' "$label" >>"$report_tmp"
  printf '$ %s\n' "$label" >>"$report_tmp"
  "$@" </dev/null >"$command_tmp" 2>&1
  command_status=$?
  redact_stream <"$command_tmp" >>"$report_tmp"
  printf '[exit status: %s]\n' "$command_status" >>"$report_tmp"
  return 0
}

command_path() {
  command_name=$1
  resolved_path=$(command -v "$command_name" 2>/dev/null || true)
  if [ -n "$resolved_path" ]; then
    record_value "$command_name" "$resolved_path"
  else
    record_value "$command_name" "not found"
  fi
}

path_status() {
  path_label=$1
  inspected_path=$2
  record_value "$path_label path" "$inspected_path"
  if [ ! -e "$inspected_path" ]; then
    record_value "$path_label status" "missing"
    return
  fi

  path_kind="other"
  if [ -d "$inspected_path" ]; then
    path_kind="directory"
  elif [ -f "$inspected_path" ]; then
    path_kind="file"
  fi
  path_access=""
  if [ -r "$inspected_path" ]; then path_access="${path_access}readable "; fi
  if [ -w "$inspected_path" ]; then path_access="${path_access}writable "; fi
  if [ -x "$inspected_path" ]; then path_access="${path_access}executable "; fi
  if [ -z "$path_access" ]; then path_access="no current-user access"; fi
  record_value "$path_label status" "$path_kind; ${path_access% }"
}

setting_value() {
  setting_name=$1
  setting_value_text=$2
  if [ -n "$setting_value_text" ]; then
    record_value "$setting_name" "$setting_value_text"
  else
    record_value "$setting_name" "unset"
  fi
}

setting_presence() {
  setting_name=$1
  setting_value_text=$2
  if [ -n "$setting_value_text" ]; then
    record_value "$setting_name" "set (value omitted)"
  else
    record_value "$setting_name" "unset"
  fi
}

plugin_status() {
  plugin_name=$1
  plugin_command=$2
  shift 2
  if ! command -v "$plugin_command" >/dev/null 2>&1; then
    record_value "$plugin_name" "CLI not found"
    return
  fi

  "$@" </dev/null >"$command_tmp" 2>&1
  plugin_status_code=$?
  if [ "$plugin_status_code" -ne 0 ]; then
    record_value "$plugin_name" "status command failed (exit $plugin_status_code)"
    redact_stream <"$command_tmp" >>"$report_tmp"
  elif grep -Eqi 'nineties(@nineties)?|nineties@nineties|karpadada/nineties' "$command_tmp"; then
    record_value "$plugin_name" "Nineties entry found"
  else
    record_value "$plugin_name" "Nineties entry not found"
  fi
}

check_database() {
  database_label=$1
  database_path=$2
  path_status "$database_label" "$database_path"
  if [ -f "$database_path" ] && command -v sqlite3 >/dev/null 2>&1; then
    capture "$database_label SQLite quick check" \
      sqlite3 -readonly -batch -noheader "$database_path" \
      "PRAGMA quick_check; PRAGMA user_version; SELECT status || ': ' || count(*) FROM collections GROUP BY status ORDER BY status;"
  fi
}

printf '%s\n' 'Nineties doctor report' >"$report_tmp"
printf '%s\n' 'This report is collected locally and is not uploaded.' >>"$report_tmp"
printf '%s\n' 'OAuth tokens, Spotify identifiers, library titles, URLs, and filenames are omitted.' >>"$report_tmp"

heading "Report"
record_value "format version" "1"
record_value "generated (UTC)" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
record_value "working directory" "$(pwd -P)"
record_value "formula" "$formula"

heading "System"
capture "uname -srm" uname -srm
if command -v sw_vers >/dev/null 2>&1; then
  capture "sw_vers" sw_vers
fi
shell_path="${SHELL:-unknown}"
record_value "shell" "${shell_path##*/}"

heading "Command paths"
for command_name in brew git curl uv python3.12 python3 ffmpeg deno sqlite3 nineties; do
  command_path "$command_name"
done

heading "Command versions"
if command -v brew >/dev/null 2>&1; then capture "brew --version" brew --version; fi
if command -v git >/dev/null 2>&1; then capture "git --version" git --version; fi
if command -v curl >/dev/null 2>&1; then capture "curl --version" curl --version; fi
if command -v uv >/dev/null 2>&1; then capture "uv --version" uv --version; fi
if command -v python3.12 >/dev/null 2>&1; then capture "python3.12 --version" python3.12 --version; fi
if command -v ffmpeg >/dev/null 2>&1; then capture "ffmpeg -version" ffmpeg -version; fi
if command -v deno >/dev/null 2>&1; then capture "deno --version" deno --version; fi
if command -v nineties >/dev/null 2>&1; then capture "nineties --version" nineties --version; fi

installed_prefix=""
if command -v brew >/dev/null 2>&1; then
  heading "Homebrew installation"
  capture "brew config" brew config
  capture "brew doctor" brew doctor
  capture "brew list --versions $formula" brew list --versions "$formula"
  capture "brew missing $formula" brew missing "$formula"
  capture "brew --prefix $formula" brew --prefix "$formula"
  installed_prefix=$(brew --prefix "$formula" </dev/null 2>/dev/null || true)

  tap_name=${formula%/*}
  tap_repository=$(brew --repository "$tap_name" </dev/null 2>/dev/null || true)
  if [ -n "$tap_repository" ] && [ -d "$tap_repository/.git" ]; then
    path_status "tap repository" "$tap_repository"
    capture "git tap status" git -C "$tap_repository" status --short --branch
    capture "git tap HEAD" git -C "$tap_repository" rev-parse HEAD
    capture "git tap origin/HEAD" git -C "$tap_repository" rev-parse origin/HEAD
    tap_git_dir=$(git -C "$tap_repository" rev-parse --absolute-git-dir </dev/null 2>/dev/null || true)
    if [ -n "$tap_git_dir" ] && { [ -d "$tap_git_dir/rebase-merge" ] || [ -d "$tap_git_dir/rebase-apply" ]; }; then
      record_value "tap rebase state" "rebase in progress"
    else
      record_value "tap rebase state" "none detected"
    fi
  else
    record_value "tap repository" "not found"
  fi

  if [ -n "${HOMEBREW_LOGS:-}" ]; then
    brew_log_directory="$HOMEBREW_LOGS/nineties"
  elif [ -n "${HOME:-}" ]; then
    brew_log_directory="$HOME/Library/Logs/Homebrew/nineties"
  else
    brew_log_directory=""
  fi
  if [ -n "$brew_log_directory" ]; then
    path_status "Homebrew Nineties logs" "$brew_log_directory"
    if [ -d "$brew_log_directory" ]; then
      log_count=0
      included_log_count=0
      for log_path in "$brew_log_directory"/* "$brew_log_directory"/*/*; do
        if [ ! -f "$log_path" ]; then continue; fi
        log_count=$((log_count + 1))
        if [ "$log_count" -gt 12 ]; then continue; fi
        included_log_count=$((included_log_count + 1))
        printf '\n### Homebrew log excerpt: ' >>"$report_tmp"
        printf '%s\n' "$log_path" | redact_stream >>"$report_tmp"
        tail -n 40 "$log_path" >"$command_tmp" 2>&1
        redact_stream <"$command_tmp" >>"$report_tmp"
      done
      record_value "Homebrew log files found" "$log_count"
      record_value "Homebrew log files included" "$included_log_count"
    fi
  fi
else
  heading "Homebrew installation"
  record_value "Homebrew" "not found; Nineties cannot be installed by the supported installer"
fi

package_root="${NINETIES_PACKAGE_ROOT:-}"
if [ -z "$package_root" ] && [ -f "$0" ]; then
  script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P || true)
  if [ -n "$script_directory" ] && [ -f "$script_directory/pyproject.toml" ]; then
    package_root=$script_directory
  fi
fi
if [ -z "$package_root" ] && [ -n "$installed_prefix" ] && [ -d "$installed_prefix/libexec" ]; then
  package_root="$installed_prefix/libexec"
fi

heading "Installed files"
if [ -n "$installed_prefix" ]; then
  path_status "formula prefix" "$installed_prefix"
  path_status "Homebrew executable" "$installed_prefix/bin/nineties"
fi
if [ -n "$package_root" ]; then
  path_status "package root" "$package_root"
  path_status "launcher" "$package_root/nineties"
  if [ ! -e "$package_root/nineties" ]; then
    path_status "checkout launcher" "$package_root/scripts/nineties"
  fi
  path_status "doctor script" "$package_root/doctor.sh"
  path_status "project metadata" "$package_root/pyproject.toml"
  path_status "dependency lock" "$package_root/uv.lock"
  if [ -f "$package_root/pyproject.toml" ]; then
    package_version=$(sed -nE 's/^version = "([^"]+)"/\1/p' "$package_root/pyproject.toml" | head -n 1)
    record_value "package version" "${package_version:-unreadable}"
  fi
else
  record_value "package root" "not found"
  package_version=""
fi

heading "Configuration"
setting_value "MUSIC_APP_DATA_DIR" "${MUSIC_APP_DATA_DIR:-}"
setting_value "MUSIC_LOCAL_DATA_DIR" "${MUSIC_LOCAL_DATA_DIR:-}"
setting_value "MUSIC_LIBRARY_DIR" "${MUSIC_LIBRARY_DIR:-}"
setting_value "MUSIC_STATE_DIR" "${MUSIC_STATE_DIR:-}"
setting_value "MUSIC_VOLUME_ROOT" "${MUSIC_VOLUME_ROOT:-}"
setting_value "MUSIC_PLAYER_VOLUME" "${MUSIC_PLAYER_VOLUME:-}"
setting_value "MUSIC_STORAGE_MODE" "${MUSIC_STORAGE_MODE:-}"
setting_value "MUSIC_SIMULATOR_DIR" "${MUSIC_SIMULATOR_DIR:-}"
setting_value "MUSIC_PORT" "${MUSIC_PORT:-}"
setting_value "MUSIC_REQUIRE_PLAYER_VOLUME" "${MUSIC_REQUIRE_PLAYER_VOLUME:-}"
setting_value "MUSIC_CREDENTIALS_DIR" "${MUSIC_CREDENTIALS_DIR:-}"
setting_presence "MUSIC_SPOTIFY_CLIENT_ID" "${MUSIC_SPOTIFY_CLIENT_ID:-}"
setting_presence "MUSIC_SPOTIFY_SUPPORT_CONTACT" "${MUSIC_SPOTIFY_SUPPORT_CONTACT:-}"
setting_value "NINETIES_UV" "${NINETIES_UV:-}"
setting_value "NINETIES_PYTHON" "${NINETIES_PYTHON:-}"

if [ -n "${MUSIC_APP_DATA_DIR:-}" ]; then
  app_data=$MUSIC_APP_DATA_DIR
elif [ -n "${XDG_DATA_HOME:-}" ]; then
  app_data="$XDG_DATA_HOME/nineties-music"
elif [ -n "${HOME:-}" ]; then
  app_data="$HOME/.local/share/nineties-music"
else
  app_data=""
fi

heading "Runtime"
if [ -n "$app_data" ]; then
  runtime_root="$app_data/runtime"
  path_status "application data" "$app_data"
  path_status "runtime root" "$runtime_root"
  if [ -d "$runtime_root" ]; then
    runtime_count=0
    for runtime_directory in "$runtime_root"/*; do
      if [ ! -d "$runtime_directory" ]; then continue; fi
      runtime_count=$((runtime_count + 1))
      runtime_name=$(basename -- "$runtime_directory")
      record_value "runtime $runtime_count" "$runtime_name"
      path_status "runtime $runtime_name ready marker" "$runtime_directory/.ready"
      path_status "runtime $runtime_name Python" "$runtime_directory/.venv/bin/python"
      path_status "runtime $runtime_name yt-dlp" "$runtime_directory/.venv/bin/yt-dlp"
      if [ -x "$runtime_directory/.venv/bin/python" ]; then
        capture "runtime $runtime_name Python imports" \
          "$runtime_directory/.venv/bin/python" -c \
          'import importlib.metadata as m; names=("flask", "yt-dlp", "yt-dlp-ejs", "ytmusicapi"); print("\n".join(f"{name} {m.version(name)}" for name in names))'
      fi
    done
    record_value "runtime directories" "$runtime_count"
  fi
else
  record_value "application data" "could not determine because HOME and XDG_DATA_HOME are unset"
fi

heading "Storage"
volume_root="${MUSIC_VOLUME_ROOT:-/Volumes}"
volume_name="${MUSIC_PLAYER_VOLUME:-Music}"
player_volume="$volume_root/$volume_name"
path_status "volume root" "$volume_root"
path_status "expected player volume" "$player_volume"
if [ -n "${MUSIC_LIBRARY_DIR:-}" ]; then
  path_status "configured library" "$MUSIC_LIBRARY_DIR"
elif [ -d "$player_volume" ]; then
  path_status "detected player library" "$player_volume/Music"
elif [ -n "${MUSIC_LOCAL_DATA_DIR:-}" ]; then
  path_status "fallback library" "$MUSIC_LOCAL_DATA_DIR/downloads"
elif [ -n "$app_data" ]; then
  path_status "fallback library" "$app_data/downloads"
fi

if [ -n "${MUSIC_STATE_DIR:-}" ]; then
  check_database "configured library database" "$MUSIC_STATE_DIR/library.sqlite3"
fi
if [ -n "$app_data" ]; then
  check_database "local library database" "$app_data/.state/library.sqlite3"
fi
if [ -d "$player_volume" ]; then
  check_database "player library database" "$player_volume/.nineties-music/library.sqlite3"
fi
if [ -n "${MUSIC_SIMULATOR_DIR:-}" ]; then
  check_database "simulator library database" "$MUSIC_SIMULATOR_DIR/Music/.nineties-music/library.sqlite3"
  check_database "simulator device database" "$MUSIC_SIMULATOR_DIR/device.sqlite3"
fi

heading "Local web app"
web_port="${MUSIC_PORT:-4310}"
case "$web_port" in
  ''|*[!0-9]*) record_value "health check" "skipped because MUSIC_PORT is not numeric" ;;
  *)
    if command -v curl >/dev/null 2>&1; then
      capture "curl local web app on port $web_port" \
        curl --silent --show-error --output /dev/null --write-out 'HTTP %{http_code}\n' \
        --max-time 3 "http://127.0.0.1:$web_port/"
    else
      record_value "health check" "skipped because curl is not installed"
    fi
    ;;
esac

heading "Agent plugins"
plugin_status "Codex plugin" codex codex plugin list --json
plugin_status "Claude plugin" claude claude plugin list --json
plugin_status "Pi plugin" pi pi list
if [ -n "${HOME:-}" ]; then
  codex_plugin_cache="${CODEX_HOME:-$HOME/.codex}/plugins/cache/nineties/nineties"
  claude_plugin_cache="$HOME/.claude/plugins/cache/nineties/nineties"
  for plugin_cache in "$codex_plugin_cache" "$claude_plugin_cache"; do
    if [ ! -d "$plugin_cache" ]; then continue; fi
    path_status "plugin cache" "$plugin_cache"
    for plugin_version_directory in "$plugin_cache"/*; do
      if [ ! -d "$plugin_version_directory" ]; then continue; fi
      plugin_version_name=$(basename -- "$plugin_version_directory")
      record_value "cached plugin version" "$plugin_version_name"
      path_status "cached plugin wrapper" \
        "$plugin_version_directory/skills/nineties/scripts/nineties"
    done
  done
fi

printf '\nEnd of report. Review this file before sharing it.\n' >>"$report_tmp"
mv -- "$report_tmp" "$report_path"
report_tmp=""
rm -f -- "$command_tmp"
command_tmp=""
trap - EXIT HUP INT TERM

echo "Nineties diagnostic report created: $report_path"
echo "Review the report before sharing it; it was not uploaded."
