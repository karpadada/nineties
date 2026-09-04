from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _json(relative: str) -> dict[str, object]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_release_versions_match_application() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(
        package
        for package in lock["package"]
        if package["name"] == "nineties-music-browser"
    )
    formula = (ROOT / "Formula/nineties.rb").read_text(encoding="utf-8")
    formula_version = re.search(r'tag: "v([^"]+)"', formula)
    assert formula_version is not None
    versions = {
        project["project"]["version"],
        locked_project["version"],
        _json(".codex-plugin/plugin.json")["version"],
        _json(".claude-plugin/plugin.json")["version"],
        formula_version.group(1),
    }
    assert len(versions) == 1


def test_homebrew_formula_has_main_branch_head() -> None:
    formula = (ROOT / "Formula/nineties.rb").read_text(encoding="utf-8")

    assert (
        'head "https://github.com/karpadada/nineties.git", branch: "main"'
        in formula
    )
    assert '(bin/"nineties").write_env_script' in formula


def test_remote_runner_refreshes_tap_and_installs_head(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    brew_log = tmp_path / "brew.log"
    app_log = tmp_path / "app.log"

    fake_brew = fake_bin / "brew"
    fake_brew.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_BREW_LOG\"\n"
        "if [ \"$*\" = 'tap' ]; then\n"
        "  printf '%s\\n' 'karpadada/nineties'\n"
        "elif [ \"$1\" = 'list' ]; then\n"
        "  exit 1\n"
        "elif [ \"$1\" = '--prefix' ]; then\n"
        "  printf '%s\\n' \"$FAKE_BREW_PREFIX\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_brew.chmod(0o755)
    fake_app = prefix / "bin/nineties"
    fake_app.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_APP_LOG\"\n",
        encoding="utf-8",
    )
    fake_app.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_BREW_LOG": str(brew_log),
            "FAKE_BREW_PREFIX": str(prefix),
            "FAKE_APP_LOG": str(app_log),
        }
    )

    subprocess.run(
        [ROOT / "run.sh", "--version"], check=True, env=environment
    )

    assert brew_log.read_text(encoding="utf-8").splitlines() == [
        "tap",
        "list --versions karpadada/nineties/nineties",
        "update",
        "install --HEAD karpadada/nineties/nineties",
        "--prefix karpadada/nineties/nineties",
    ]
    assert app_log.read_text(encoding="utf-8").splitlines() == ["--version"]


def test_remote_runner_upgrades_an_existing_app_before_web_start(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    brew_log = tmp_path / "brew.log"
    app_log = tmp_path / "app.log"

    fake_brew = fake_bin / "brew"
    fake_brew.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_BREW_LOG\"\n"
        "if [ \"$*\" = 'tap' ]; then\n"
        "  printf '%s\\n' 'karpadada/nineties'\n"
        "elif [ \"$1\" = '--prefix' ]; then\n"
        "  printf '%s\\n' \"$FAKE_BREW_PREFIX\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_brew.chmod(0o755)
    fake_app = prefix / "bin/nineties"
    fake_app.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"${*:-web}\" >> \"$FAKE_APP_LOG\"\n",
        encoding="utf-8",
    )
    fake_app.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_BREW_LOG": str(brew_log),
            "FAKE_BREW_PREFIX": str(prefix),
            "FAKE_APP_LOG": str(app_log),
        }
    )

    subprocess.run([ROOT / "run.sh"], check=True, env=environment)

    assert brew_log.read_text(encoding="utf-8").splitlines() == [
        "tap",
        "list --versions karpadada/nineties/nineties",
        "update",
        "upgrade --fetch-HEAD karpadada/nineties/nineties",
        "--prefix karpadada/nineties/nineties",
    ]
    assert app_log.read_text(encoding="utf-8").splitlines() == ["web"]


def test_remote_runner_repairs_legacy_install_without_executable(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    brew_log = tmp_path / "brew.log"
    app_log = tmp_path / "app.log"
    app_source = tmp_path / "nineties"
    app_source.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_APP_LOG\"\n",
        encoding="utf-8",
    )
    app_source.chmod(0o755)

    fake_brew = fake_bin / "brew"
    fake_brew.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_BREW_LOG\"\n"
        "if [ \"$*\" = 'tap' ]; then\n"
        "  printf '%s\\n' 'karpadada/nineties'\n"
        "elif [ \"$1\" = '--prefix' ]; then\n"
        "  printf '%s\\n' \"$FAKE_BREW_PREFIX\"\n"
        "elif [ \"$1\" = 'reinstall' ]; then\n"
        "  mkdir -p \"$FAKE_BREW_PREFIX/bin\"\n"
        "  cp \"$FAKE_APP_SOURCE\" \"$FAKE_BREW_PREFIX/bin/nineties\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_brew.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_BREW_LOG": str(brew_log),
            "FAKE_BREW_PREFIX": str(prefix),
            "FAKE_APP_LOG": str(app_log),
            "FAKE_APP_SOURCE": str(app_source),
        }
    )

    subprocess.run(
        [ROOT / "run.sh", "--version"], check=True, env=environment
    )

    assert brew_log.read_text(encoding="utf-8").splitlines() == [
        "tap",
        "list --versions karpadada/nineties/nineties",
        "--prefix karpadada/nineties/nineties",
        "update",
        "reinstall karpadada/nineties/nineties",
        "--prefix karpadada/nineties/nineties",
    ]
    assert app_log.read_text(encoding="utf-8").splitlines() == ["--version"]


def test_marketplaces_publish_the_repository_root_plugin() -> None:
    codex = _json(".agents/plugins/marketplace.json")
    claude = _json(".claude-plugin/marketplace.json")

    codex_plugin = codex["plugins"][0]
    assert codex_plugin["name"] == "nineties"
    assert codex_plugin["source"]["source"] == "url"
    assert codex_plugin["source"]["url"].endswith("karpadada/nineties.git")

    claude_plugin = claude["plugins"][0]
    assert claude_plugin["name"] == "nineties"
    assert claude_plugin["source"] == {
        "source": "github",
        "repo": "karpadada/nineties",
    }


def test_skill_uses_shared_homebrew_executable_without_mcp_bundle() -> None:
    wrapper = ROOT / "skills" / "nineties" / "scripts" / "nineties"
    assert os.access(wrapper, os.X_OK)
    contents = wrapper.read_text(encoding="utf-8")
    assert "command -v nineties" in contents
    assert "Homebrew" in contents
    assert 'exec "$executable" agent "$@"' in contents
    assert "nix" not in contents.lower()
    assert not (ROOT / ".mcp.json").exists()


def test_skill_calls_shared_nineties_executable(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    executable_log = tmp_path / "executable.log"
    fake_executable = fake_bin / "nineties"
    fake_executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_EXECUTABLE_LOG\"\n",
        encoding="utf-8",
    )
    fake_executable.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_EXECUTABLE_LOG": str(executable_log),
        }
    )
    wrapper = ROOT / "skills" / "nineties" / "scripts" / "nineties"

    subprocess.run(
        [wrapper, "search", "Fictional Album"], check=True, env=environment
    )
    subprocess.run([wrapper, "library"], check=True, env=environment)

    assert executable_log.read_text(encoding="utf-8").splitlines() == [
        "agent search Fictional Album",
        "agent library",
    ]


def test_homebrew_launcher_updates_per_session_and_every_web_start(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    python_log = tmp_path / "python.log"
    player_required_log = tmp_path / "player-required.log"
    codex_log = tmp_path / "codex.log"
    runtime_python = tmp_path / "runtime-python"
    runtime_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_PYTHON_LOG\"\n"
        "printf '%s\\n' \"${MUSIC_REQUIRE_PLAYER_VOLUME:-}\" "
        ">> \"$FAKE_PLAYER_REQUIRED_LOG\"\n",
        encoding="utf-8",
    )
    runtime_python.chmod(0o755)

    fake_python = fake_bin / "python3.12"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_UV_LOG\"\n"
        "mkdir -p \"$UV_PROJECT_ENVIRONMENT/bin\"\n"
        "cp \"$FAKE_RUNTIME_PYTHON\" \"$UV_PROJECT_ENVIRONMENT/bin/python\"\n"
        "touch \"$UV_PROJECT_ENVIRONMENT/bin/yt-dlp\"\n"
        "chmod +x \"$UV_PROJECT_ENVIRONMENT/bin/yt-dlp\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CODEX_LOG\"\n"
        "if [[ \"$*\" == 'plugin list --json' ]]; then\n"
        "  printf '%s\\n' "
        "'{\"installed\":[{\"pluginId\":\"nineties@nineties\"}]}'\n"
        "elif [[ \"$*\" == 'plugin marketplace list --json' ]]; then\n"
        "  printf '%s\\n' "
        "'{\"marketplaces\":[{\"name\":\"nineties\",'"
        "'\"marketplaceSource\":{\"source\":'"
        "'\"https://github.com/karpadada/nineties.git\"}}]}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    for command_name in ("claude", "pi"):
        fake_command = fake_bin / command_name
        fake_command.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_command.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "MUSIC_APP_DATA_DIR": str(tmp_path / "app-data"),
            "FAKE_UV_LOG": str(uv_log),
            "FAKE_PYTHON_LOG": str(python_log),
            "FAKE_PLAYER_REQUIRED_LOG": str(player_required_log),
            "FAKE_CODEX_LOG": str(codex_log),
            "FAKE_RUNTIME_PYTHON": str(runtime_python),
            "NINETIES_AGENT_SESSION_ID": "session-one",
        }
    )
    launcher = ROOT / "scripts" / "nineties"

    subprocess.run(
        [launcher, "agent", "search", "Fictional Album"],
        check=True,
        env=environment,
    )
    subprocess.run([launcher, "agent", "library"], check=True, env=environment)

    environment["NINETIES_AGENT_SESSION_ID"] = "session-two"
    subprocess.run([launcher, "agent", "status"], check=True, env=environment)
    subprocess.run([launcher, "web"], check=True, env=environment)
    subprocess.run([launcher, "web"], check=True, env=environment)

    uv_calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert len(uv_calls) == 4
    assert "--upgrade-package yt-dlp" in uv_calls[0]
    assert "--upgrade-package yt-dlp-ejs" in uv_calls[0]
    assert "--upgrade-package ytmusicapi" in uv_calls[0]
    assert python_log.read_text(encoding="utf-8").splitlines() == [
        "-m nineties_music agent search Fictional Album",
        "-m nineties_music agent library",
        "-m nineties_music agent status",
        "-m nineties_music web",
        "-m nineties_music web",
    ]
    assert player_required_log.read_text(encoding="utf-8").splitlines() == [
        "",
        "",
        "",
        "1",
        "1",
    ]
    assert codex_log.read_text(encoding="utf-8").splitlines() == [
        "plugin list --json",
        "plugin marketplace list --json",
        "plugin marketplace upgrade nineties",
        "plugin add nineties@nineties",
        "plugin list --json",
        "plugin marketplace list --json",
        "plugin marketplace upgrade nineties",
        "plugin add nineties@nineties",
    ]


def test_skill_passes_codex_thread_as_agent_session(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    executable_log = tmp_path / "executable.log"
    fake_executable = fake_bin / "nineties"
    fake_executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s\\n' \"${NINETIES_AGENT_SESSION_ID:-}\" \"$*\" "
        ">> \"$FAKE_EXECUTABLE_LOG\"\n",
        encoding="utf-8",
    )
    fake_executable.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_EXECUTABLE_LOG": str(executable_log),
            "CODEX_THREAD_ID": "thread-123",
        }
    )
    environment.pop("NINETIES_AGENT_SESSION_ID", None)

    subprocess.run(
        [ROOT / "skills/nineties/scripts/nineties", "library"],
        check=True,
        env=environment,
    )

    assert executable_log.read_text(encoding="utf-8").strip() == (
        "thread-123|agent library"
    )


def test_plugin_install_convenience_configures_codex(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex_log = tmp_path / "codex.log"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CODEX_LOG\"\n"
        "if [[ \"$*\" == 'plugin marketplace list --json' ]]; then\n"
        "  printf '%s\\n' '{\"marketplaces\": []}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_CODEX_LOG": str(codex_log),
        }
    )

    subprocess.run(
        [ROOT / "scripts/nineties", "plugins", "install", "codex"],
        check=True,
        env=environment,
    )

    assert codex_log.read_text(encoding="utf-8").splitlines() == [
        "plugin marketplace list --json",
        "plugin marketplace add karpadada/nineties",
        "plugin add nineties@nineties",
    ]


def test_update_hands_off_to_new_homebrew_executable(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    brew_log = tmp_path / "brew.log"
    handoff_log = tmp_path / "handoff.log"

    fake_brew = fake_bin / "brew"
    fake_brew.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_BREW_LOG\"\n"
        "if [[ \"$1\" == '--prefix' ]]; then\n"
        "  printf '%s\\n' \"$FAKE_BREW_PREFIX\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_brew.chmod(0o755)
    updated_executable = prefix / "bin/nineties"
    updated_executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_HANDOFF_LOG\"\n",
        encoding="utf-8",
    )
    updated_executable.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_BREW_LOG": str(brew_log),
            "FAKE_BREW_PREFIX": str(prefix),
            "FAKE_HANDOFF_LOG": str(handoff_log),
        }
    )

    subprocess.run(
        [ROOT / "scripts/nineties", "update"], check=True, env=environment
    )

    assert brew_log.read_text(encoding="utf-8").splitlines() == [
        "list --versions karpadada/nineties/nineties",
        "update",
        "upgrade --fetch-HEAD karpadada/nineties/nineties",
        "--prefix karpadada/nineties/nineties",
    ]
    assert handoff_log.read_text(encoding="utf-8").splitlines() == [
        "__update-installed-plugins"
    ]


def test_plugin_update_refreshes_installed_codex_plugin(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex_home = tmp_path / "codex-home"
    (codex_home / "plugins/cache/nineties/nineties/0.3.0").mkdir(parents=True)
    codex_log = tmp_path / "codex.log"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CODEX_LOG\"\n"
        "if [[ \"$*\" == 'plugin list --json' ]]; then\n"
        "  printf '%s\\n' '{\"installed\": []}'\n"
        "elif [[ \"$*\" == 'plugin marketplace list --json' ]]; then\n"
        "  printf '%s\\n' "
        "'{\"marketplaces\":[{\"name\":\"nineties\",'"
        "'\"marketplaceSource\":{\"source\":'"
        "'\"https://github.com/karpadada/nineties.git\"}}]}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "CODEX_HOME": str(codex_home),
            "FAKE_CODEX_LOG": str(codex_log),
        }
    )

    subprocess.run(
        [ROOT / "scripts/nineties", "plugins", "update", "codex"],
        check=True,
        env=environment,
    )

    assert codex_log.read_text(encoding="utf-8").splitlines() == [
        "plugin list --json",
        "plugin marketplace list --json",
        "plugin marketplace upgrade nineties",
        "plugin add nineties@nineties",
    ]


def test_mcp_is_not_a_user_facing_command() -> None:
    launcher = (ROOT / "scripts/nineties").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    main = (ROOT / "src/nineties_music/__main__.py").read_text(encoding="utf-8")

    assert " mcp" not in launcher.lower()
    assert "mcp" not in readme.lower()
    assert '"mcp"' not in main


def test_runtime_pruning_keeps_two_recent_and_active_versions(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtimes"
    runtime_root.mkdir()
    versions = []
    for index in range(3):
        version = runtime_root / f"version-{index}"
        version.mkdir()
        (version / ".nineties-runtime").touch()
        last_used = version / ".last-used"
        last_used.touch()
        os.utime(last_used, (index + 1, index + 1))
        versions.append(version)

    active_marker = versions[0] / f".active.{os.getpid()}"
    active_marker.touch()
    prune = ROOT / "scripts" / "prune-runtime-versions"

    subprocess.run([prune, runtime_root, "2"], check=True)
    assert all(version.exists() for version in versions)

    active_marker.unlink()
    subprocess.run([prune, runtime_root, "2"], check=True)
    assert not versions[0].exists()
    assert versions[1].exists()
    assert versions[2].exists()
