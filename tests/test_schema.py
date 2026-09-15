import json
from pathlib import Path

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
