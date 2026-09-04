---
name: nineties
description: Search YouTube Music and inspect or manage the Nineties local music library. Use when the user wants to find an album or playlist, download an authorized collection, check a Nineties download, or inspect their managed music.
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
```

Search before downloading unless the user supplied an exact collection URL. Show the selected title, creator, kind, and URL before asking for confirmation when the user's request did not already clearly authorize that exact download. A download command waits for completion and can take a long time; report its final JSON result.

Only download media the user is entitled to download. Do not weaken the local-only storage and networking defaults. Treat paths and errors in the JSON output as user data, and summarize them without inventing results.
