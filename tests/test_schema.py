"""The committed config.schema.json, and main's CLI: schema, serve, and exit codes."""

import json
from pathlib import Path

import pytest

from mcp_school.config import schema
from mcp_school.main import build_parser, main

ROOT = Path(__file__).resolve().parents[1]


def test_the_committed_schema_is_the_model():
    committed = json.loads((ROOT / "config.schema.json").read_text())
    assert committed == schema(), (
        "config.schema.json is stale: run `mcp-school schema > config.schema.json`"
    )


def test_the_schema_subcommand_prints_the_model(capsys):
    main(["schema"])
    assert json.loads(capsys.readouterr().out) == schema()


def test_serve_is_the_default_command():
    assert build_parser().parse_args([]).command == "serve"


def test_serve_with_a_missing_config_exits_2_with_a_message_on_stderr(capsys):
    """A bad config must fail loudly at boot, not silently serve nothing."""
    with pytest.raises(SystemExit) as raised:
        main(["serve", "--config", "/nope"])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "mcp-school:" in err and "/nope" in err
