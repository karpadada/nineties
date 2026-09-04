# Repository Instructions

## Plugin Versioning

- Any change to shipped application behavior, agent integration, plugin metadata,
  or packaged plugin contents must include an appropriate semantic version bump.
- Use `python3 scripts/set_version.py <version>` so the Python package,
  lockfile, Homebrew formula, Codex manifest, and Claude manifest stay
  synchronized.
- Treat incompatible storage or API changes in a pre-1.0 release as a minor
  version bump. Use patch bumps for backward-compatible fixes and minor bumps
  for backward-compatible features.
- Run `uv run --locked pytest`, `brew style Formula/nineties.rb`, and the plugin
  validators after the bump.
