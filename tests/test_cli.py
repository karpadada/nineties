from __future__ import annotations

import json

from nineties_music import cli
from nineties_music.config import AppConfig

from test_agent import make_api


def test_agent_cli_search_outputs_json(tmp_path, monkeypatch, capsys) -> None:
    api, _, _ = make_api(tmp_path)
    monkeypatch.setattr(cli, "create_services", lambda *_args, **_kwargs: api.services)

    result = cli.run_agent_cli(
        AppConfig(
            project_root=tmp_path,
            library_dir=tmp_path / "music",
            state_dir=tmp_path / "state",
        ),
        ["search", "Fictional Album", "--limit", "1"],
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["title"] == "Fictional Album"


def test_agent_cli_reports_domain_errors_as_json(tmp_path, monkeypatch, capsys) -> None:
    api, _, _ = make_api(tmp_path)
    monkeypatch.setattr(cli, "create_services", lambda *_args, **_kwargs: api.services)

    result = cli.run_agent_cli(
        api.services.config,
        ["status", "missing"],
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert "job ID" in payload["error"]
