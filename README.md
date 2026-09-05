# Nineties — 90s Music Browser

A deliberately plain local website for finding YouTube Music albums and
playlists and copying them to an MP3-player-friendly music directory. Discovery
uses `ytmusicapi`; downloads, conversion, metadata, and cover art are handled by
`yt-dlp` and FFmpeg.

## Run Nineties

```sh
curl -fsSL https://raw.githubusercontent.com/karpadada/nineties/main/run.sh | sh
```

Open <http://127.0.0.1:4310>. The repository's `run.sh` uses Homebrew to install
Nineties when needed. On later runs it upgrades the application, refreshes any
installed Nineties agent plugins, and then starts the web app.

Optionally, install the plugin for every supported AI agent already on your
computer:

```sh
nineties plugins install all
```

The interface has no stylesheet. It is intended to feel like a small, useful
website from the 1990s: headings, forms, tables, links, and status text.

It can:

- search YouTube Music for albums and playlists, with cover art;
- download MP3 collections with progress, retry, and removal controls;
- keep a transactional library database alongside a supported removable music card; and
- expose search, download, status, library, and safe-device-removal operations
  to AI agents through on-demand plugins.

> Only download media you are entitled to download. YouTube and YouTube Music
> may change independently of this project, and their terms still apply.

## Requirements

- Homebrew
- Network access on the first run and for the startup compatibility update

No separate Python, uv, `yt-dlp`, FFmpeg, or JavaScript runtime installation is
required. The Homebrew formula provides the complete runtime toolchain,
including Deno for `yt-dlp`'s YouTube JavaScript challenge handling.

## Manual installation

Install Nineties directly from its GitHub-hosted Homebrew tap:

```sh
brew tap karpadada/nineties https://github.com/karpadada/nineties.git
brew install --HEAD karpadada/nineties/nineties
nineties
```

Open <http://127.0.0.1:4310>. The first run can take a little longer while the
runtime environment is created.

When a `Music` volume is mounted under `/Volumes`, the app automatically
uses its `Music` directory and stores its database in `.nineties-music` on the
device. If the installed web app starts without that volume, it prominently
disables downloads instead of silently using local storage. Connect the disk
and restart Nineties. Direct Python development uses `./downloads/` and
`./.state/`.
Paths can also be selected explicitly:

```sh
MUSIC_LIBRARY_DIR="/Volumes/Music/Music" \
MUSIC_STATE_DIR="/Volumes/Music/.nineties-music" \
nineties
```

The database then travels with the card. Use **Safely remove** in the web app,
or ask an AI agent using the Nineties plugin to safely remove the music device,
before disconnecting it. Safe removal refuses to eject the device while a
download or collection-removal operation is active.
Nineties recognizes the configured `Music` volume by exact name, then uses
macOS device metadata to eject only removable external disks exposed by the
same physical USB player. This includes the player's `ECHO NANO` volume without
ejecting unrelated external disks.
Open Nineties pages continue checking the connection state and reload after the
same player is reconnected and its on-device library database is available.

The launcher checks for compatible updates to `ytmusicapi`, `yt-dlp`, and
`yt-dlp-ejs` whenever the web API starts and on the first Nineties skill call in
each agent session. A YouTube search, inspection, or download failure also
triggers an immediate update attempt and one retry. The other application
dependencies remain pinned by the repository lockfile. The writable runtime is
stored under `$XDG_DATA_HOME/nineties-music/runtime` (normally
`~/.local/share/nineties-music/runtime`). If an update fails, the application
restores and uses its last working runtime. A first launch still requires a
network connection. Nineties automatically keeps the two most recently used
successful runtime versions and removes older inactive versions.

One-shot agent commands continue to use local app-data storage when no supported
player is mounted. The installed web app requires the player unless an explicit
`MUSIC_LIBRARY_DIR` is configured.

## Use without a physical player

Keep using Nineties as a local MP3 library builder:

```sh
nineties web --local
nineties agent --local library
```

Local mode ignores mounted players, uses the existing local-data `downloads`
and `.state` directories, and has no eject operation. `MUSIC_LIBRARY_DIR` and
`MUSIC_STATE_DIR` can override those paths. Set `MUSIC_STORAGE_MODE=local` in
your environment to select it for both the web app and agent commands.

## Virtual player

Start a persistent virtual player for development and support:

```sh
nineties simulator
```

It starts connected on first use. The web app has **Connect**, **Disconnect**,
and **Safely remove virtual player** controls. Connection changes are visible
to other web and agent processes, and reconnecting restores the same library
without restarting the web app. Restarting preserves the last connection state.
Disconnect and safe removal both refuse while a download or collection removal
is active. They keep the files and never invoke macOS disk ejection.

```sh
nineties simulator status
nineties simulator disconnect
nineties simulator connect
nineties agent --simulator library
nineties agent --simulator safely-remove
```

The default directory is `simulator` inside the local-data root. It contains
`Music/Music` for audio, `Music/.nineties-music` for the real library database,
and `device.sqlite3` for the simulated connection. To keep separate test
players, use `nineties simulator --directory /path/to/test-player` and
`nineties agent --simulator-dir /path/to/test-player library`, or set
`MUSIC_SIMULATOR_DIR` consistently. `nineties web --simulator` and
`MUSIC_STORAGE_MODE=simulator` select the same mode. Simulator mode always
uses its own music and state paths, even if physical-device path overrides
are present in the environment.

This is a folder-based storage simulator: search, download, conversion, tags,
library reconciliation, retry, removal, and concurrent agent operations use
the real application. It does not emulate USB, player firmware, playback,
FAT/exFAT, capacity limits, or pulling a cable during a write. It uses the host
filesystem and free space, and downloads still require network access.

Before retiring the physical player, preserve sanitized disk metadata and
filesystem details, and record a playback check using a small test collection.
The existing storage tests cover the player's companion-volume ejection logic;
firmware playback compatibility still needs an occasional physical-device check.

From a checkout, run `PYTHONPATH=src uv run --locked python -m nineties_music simulator`
to exercise the web app without the Homebrew launcher's startup updates. The
simulator tests use isolated temporary directories and fixture downloads, so
`uv run --locked pytest tests/test_simulator.py` needs no player or YouTube access
once the locked development dependencies are installed.

## Use with an AI agent

The repository is a plugin marketplace and an Agent Skills package. The
installed `nineties` skill is discovered at agent startup, but it calls the
Homebrew-installed application only when a matching request needs it. The web
interface and every agent plugin therefore use the same application runtime.

Install plugins for every supported agent already present on the computer:

```sh
nineties plugins install all
```

### Codex

```sh
nineties plugins install codex
```

### Claude Code

```sh
nineties plugins install claude
```

### Pi

```sh
nineties plugins install pi
```

Start a new agent session after installation. Ask it something like: “Use
Nineties to search for *Fictional Album* by *Fictional Artist*,” or “Use
Nineties to safely remove my music device.” When invoking
the skill explicitly, use `$nineties` in Codex, `/nineties:nineties` in Claude
Code, or `/skill:nineties` in Pi.

Only download media you are entitled to download. Names used in examples and
test fixtures are fictional placeholders and do not refer to real music.

## Update

One command upgrades the Homebrew application and every installed Nineties
agent plugin:

```sh
nineties update
```

To refresh plugins without upgrading the application, or to target one agent:

```sh
nineties plugins update all
nineties plugins update codex
```

Existing agent sessions continue using the plugin version they loaded. Begin a
new session after installing or updating a plugin.

The web app and agent plugins can run at the same time. Plugin commands use
short-lived Nineties processes against the same WAL-mode SQLite database; leases
and transactional reservations prevent concurrent downloads from claiming the
same collection or directory.

## Configuration

The Homebrew launcher and app accept these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MUSIC_LIBRARY_DIR` | SD-card `Music`, or local-data `downloads` | Managed music library |
| `MUSIC_STATE_DIR` | SD-card `.nineties-music`, or local-data `.state` | Library database directory |
| `MUSIC_VOLUME_ROOT` | `/Volumes` | Root searched for a mounted player |
| `MUSIC_PLAYER_VOLUME` | `Music` | Player volume name to detect |
| `MUSIC_PORT` | `4310` | Web server port |
| `MUSIC_LOCAL_DATA_DIR` | Project directory locally; app data directory when installed | Fallback music and state root |
| `MUSIC_APP_DATA_DIR` | `$XDG_DATA_HOME/nineties-music`, or `~/.local/share/nineties-music` | Writable runtime and local-data root |
| `MUSIC_REQUIRE_PLAYER_VOLUME` | Enabled by the installed web launcher | Disable web downloads unless the player was mounted at startup |
| `MUSIC_STORAGE_MODE` | `auto` | Select automatic player discovery, `local`, or `simulator` storage |
| `MUSIC_SIMULATOR_DIR` | Local-data `simulator` | Persistent virtual player root; used in simulator mode |

`MUSIC_YT_DLP` can override the downloader executable for local development.
The Homebrew launcher sets it automatically to the executable in its managed
runtime.

The web server always binds to `127.0.0.1`. The bind address cannot be changed
through configuration because the app has no authentication and is not intended
to be exposed to a local network or the internet.

Albums are stored as `Artist/Album/NN - Track.mp3`, without a YouTube source ID
in the folder name. Playlists are stored as
`Playlists/Playlist [source-id]/NN - Track.mp3`. Each imported
collection owns its directory, so the same recording may exist in an album and
a playlist. Removing a collection deletes only its managed directory.

Files changed outside the app are reported as missing. Unrelated files in the
music directory are ignored and are never deleted.

Failed, interrupted, and partial collections can be resumed with **Retry
download** on their detail page. Active detail pages refresh every two seconds,
so a terminal failure is shown instead of leaving a stale 0% display.

The library database supports multiple Nineties processes on the same computer.
Separate agent sessions may download different collections concurrently. A
transactional source and directory reservation prevents two sessions from
downloading the same collection or writing into the same managed directory.
Each running download renews an ownership lease so another process cannot
overwrite its progress or completion state.

## Privacy

Nineties has no user accounts, analytics, or telemetry, and it does not send
data to the project maintainers. It does make the network requests needed for
its features:

- search queries are sent to YouTube Music through `ytmusicapi`, which returns
  collection information;
- downloads are requested from YouTube through `yt-dlp`;
- cover art is fetched from allowlisted YouTube and Google image hosts;
- the launcher contacts package registries when the web API starts, on the first
  skill call in an agent session, and after a YouTube compatibility failure; and
- `nineties update` contacts Homebrew and configured agent marketplaces.

The local `library.sqlite3` database records collection titles, artists or
curators, source URLs and IDs, managed filenames, download status and errors,
and timestamps. It is stored in the configured state directory, which may be
on the removable music device. The local web interface displays the configured
music-directory path. Anyone with access to that device or local account may be
able to read this information, so treat the database and music library as
private data and erase them before transferring the device to someone else.

The server is restricted to the local computer at `127.0.0.1`. Do not place it
behind a proxy or otherwise expose it to another device; it has no
authentication.

## Development

Install the development tools and run the locked test suite:

```sh
brew install uv python@3.12 ffmpeg deno
uv run --locked pytest
```

After changing dependencies, update the uv lock file:

```sh
uv lock
```

Check the Homebrew formula and both plugin manifests:

```sh
brew style Formula/nineties.rb
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
claude plugin validate .
```

Python packages are declared in `pyproject.toml` and pinned in `uv.lock`.

To prepare a release, set one semantic version across the application, Homebrew
formula, and both plugin manifests, run the checks above, commit, tag, and push:

```sh
python3 scripts/set_version.py 0.4.0
git tag v0.4.0
```

## Disclaimer

Nineties is an independent project and is not affiliated with, endorsed by, or
sponsored by YouTube, YouTube Music, or Google. It is provided as-is, without
warranty. You are responsible for complying with applicable laws, service
terms, and copyright restrictions, and for downloading only media you are
authorized to use. Back up important files before allowing the application to
manage a music library or removable device.

## License

This project is available under the [MIT License](LICENSE).
