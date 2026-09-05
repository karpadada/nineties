---
name: nineties
description: Search YouTube Music and inspect or manage the Nineties local music library. Use when the user wants to find an album or playlist, download an authorized collection, check a Nineties download, inspect their managed music, or safely remove the connected music device.
---

# Nineties

Use the bundled wrapper to call the one-shot Nineties CLI. Locate
`scripts/nineties` relative to this `SKILL.md`; it invokes the Homebrew-installed
`nineties` executable and prints a clear installation error when it is absent.
The wrapper also passes through the host agent's session identifier so the
launcher performs its YouTube compatibility update on the first skill call in
that session.

The CLI prints JSON:

```sh
<skill-directory>/scripts/nineties search "artist or collection" --limit 8
<skill-directory>/scripts/nineties download "https://music.youtube.com/playlist?list=..." --kind album
<skill-directory>/scripts/nineties status [job-id]
<skill-directory>/scripts/nineties library [--query "text"] [--limit 50]
<skill-directory>/scripts/nineties safely-remove
```

Search before downloading unless the user supplied an exact collection URL. Show the selected title, creator, kind, and URL before asking for confirmation when the user's request did not already clearly authorize that exact download. A download command waits for completion and can take a long time; report its final JSON result.

Run `safely-remove` only when the user explicitly asks to eject or safely remove the connected music device. It refuses while a download or collection-removal operation is active. Report the final JSON result before telling the user it is safe to disconnect the device.

When the user selects a local library or virtual player, use that same storage
selection on every command. The wrapper accepts `--local` or `--simulator`
before the command, for example `scripts/nineties --local library` or
`scripts/nineties --simulator --simulator-dir /path/to/player library`.
`MUSIC_STORAGE_MODE=local|simulator` and `MUSIC_SIMULATOR_DIR` also apply.
Local mode stores files on the computer and has no eject operation. Simulator
mode uses real files with a persistent simulated connection state; safe removal
disconnects only that virtual player. To reconnect it, run
`nineties simulator connect [--directory /path/to/player]`. Use
`nineties simulator status` to check its connection. Do not switch away from a
disconnected player to complete a download without the user's direction.

Only download media the user is entitled to download. Do not weaken the local-only storage and networking defaults. Treat paths and errors in the JSON output as user data, and summarize them without inventing results.
