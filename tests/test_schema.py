"""The committed config.schema.json, and main's CLI: schema, serve, and exit codes."""

import json
from pathlib import Path

import pytest

from kubed.mcp_kb.config import schema
from kubed.mcp_kb.main import build_parser, main

ROOT = Path(__file__).resolve().parents[1]


def test_the_committed_schema_is_the_model():
    committed = json.loads((ROOT / "config.schema.json").read_text())
    assert committed == schema(), (
        "config.schema.json is stale: run `mcp-kb schema > config.schema.json`"
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
    assert "mcp-kb:" in err and "/nope" in err


def test_log_level_comes_from_the_environment_and_a_flag_beats_it(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert build_parser().parse_args([]).log_level == "DEBUG"
    assert build_parser().parse_args(["--log-level", "error"]).log_level == "ERROR"
    monkeypatch.delenv("LOG_LEVEL")
    assert build_parser().parse_args([]).log_level == "INFO"


def test_one_level_and_one_handler_reach_fastmcp_and_uvicorn_too():
    """Loggers that arrived with handlers of their own write through the root's."""
    import logging

    import fastmcp  # noqa: F401 - importing it is what installs its handler

    from kubed.mcp_kb.main import THIRD_PARTY_LOGGERS, configure_logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    third = {
        name: (lg.handlers[:], lg.propagate, lg.level)
        for name in THIRD_PARTY_LOGGERS
        for lg in [logging.getLogger(name)]
    }
    try:
        configure_logging("WARNING")
        assert root.level == logging.WARNING
        for name in THIRD_PARTY_LOGGERS:
            logger = logging.getLogger(name)
            assert logger.handlers == [] and logger.propagate
        assert not logging.getLogger("fastmcp.server").isEnabledFor(logging.INFO)
        assert logging.getLogger("fastmcp.server").isEnabledFor(logging.WARNING)
        assert not logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)

        configure_logging("DEBUG")
        assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])
        for name, (handlers, propagate, level) in third.items():
            logger = logging.getLogger(name)
            logger.handlers[:], logger.propagate = handlers, propagate
            logger.setLevel(level)
