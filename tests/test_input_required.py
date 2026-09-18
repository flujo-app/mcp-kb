"""A missing required argument, asked for rather than refused (§C1.37).

``prompts/get`` without a required argument is the caller's mistake and has
been -32602 naming the argument since #20. On a connection that can be asked --
MCP 2026-07-28, and a client declaring form elicitation -- it is a question
instead: the render returns an ``InputRequiredResult`` (SEP-2322) carrying one
form, and the round that answers renders.

Everything that cannot be asked keeps the error, and there are three of them:
an older protocol, a client that declares no elicitation, and the tool mirror,
whose result shape has nowhere to put a question.
"""

import logging

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config

pytestmark = pytest.mark.integration

REFUSAL = "Prompt 'kit_debug' needs the argument app."


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "plugin"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "debug.md").write_text(
        "---\n"
        "description: Debug an app.\n"
        "arguments:\n"
        "- name: app\n"
        "  description: The workload.\n"
        "  required: true\n"
        "- name: since\n"
        "  default: 1h\n"
        "---\n"
        "Look at {{ app }} since {{ since }}.\n"
    )
    config = Config.model_validate(
        {
            "plugins": [{"name": "kit", "source": f"file://{root}"}],
            "libraries": [{"name": "kit", "plugins": ["kit"]}],
        }
    )
    return KnowledgeBase(config, tmp_path / "cache")


def _answers(reply, asked):
    """An elicitation handler that records what it was asked and answers."""

    async def handler(message, response_type, params, context):
        asked.append((message, params.requested_schema))
        return reply

    return handler


def text(result):
    return result.messages[0].content.text


async def test_a_missing_argument_is_asked_for_and_then_rendered(server):
    asked = []
    handler = _answers({"app": "nextcloud"}, asked)
    async with Client(server.mcp, elicitation_handler=handler) as client:
        rendered = await client.get_prompt("kit_debug", {})

    assert text(rendered) == "Look at nextcloud since 1h.\n"
    [(message, schema)] = asked
    assert message == "The kit_debug prompt needs app."
    assert schema == {
        "type": "object",
        "properties": {"app": {"type": "string", "description": "The workload."}},
        "required": ["app"],
    }


async def test_an_answer_joins_the_arguments_already_given(server):
    """The round that answers renders the answer *and* what the first round
    sent, which is the only thing that round is still good for."""
    asked = []
    handler = _answers({"app": "penpot"}, asked)
    async with Client(server.mcp, elicitation_handler=handler) as client:
        rendered = await client.get_prompt("kit_debug", {"since": "24h"})
    assert text(rendered) == "Look at penpot since 24h.\n"
    assert len(asked) == 1


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_a_declined_or_cancelled_answer_is_the_refusal_it_started_as(
    server, action, caplog
):
    # Declined *with* content: the action is the answer, not the payload.
    handler = _answers(ElicitResult(action=action, content={"app": "sneaky"}), [])
    caplog.clear()
    with caplog.at_level(logging.INFO):
        async with Client(server.mcp, elicitation_handler=handler) as client:
            with pytest.raises(MCPError) as caught:
                await client.get_prompt("kit_debug", {})
    assert caught.value.error.code == INVALID_PARAMS
    assert caught.value.error.message == REFUSAL
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


async def test_a_client_that_declares_no_elicitation_is_refused_as_before(server):
    """No handler, no capability: the -32602 that names the argument."""
    async with Client(server.mcp) as client:
        with pytest.raises(MCPError) as caught:
            await client.get_prompt("kit_debug", {})
    assert caught.value.error.code == INVALID_PARAMS
    assert caught.value.error.message == REFUSAL


async def test_an_older_protocol_is_refused_however_capable_the_client_is(server):
    """The multi-round-trip result type exists only at 2026-07-28, so a client
    that can elicit over the initialize handshake still gets the error."""
    asked = []
    handler = _answers({"app": "nextcloud"}, asked)
    async with Client(server.mcp, elicitation_handler=handler, mode="legacy") as client:
        assert client.protocol_version not in ("2026-07-28",)
        with pytest.raises(MCPError) as caught:
            await client.get_prompt("kit_debug", {})
    assert caught.value.error.message == REFUSAL
    assert asked == []


async def test_the_tool_mirror_still_answers_with_the_error_that_says_what_to_do(
    server,
):
    """``get_prompt`` as a tool returns FastMCP's formatted messages, which an
    ask has none of -- so the mirror asks nobody and keeps the error result."""
    asked = []
    handler = _answers({"app": "nextcloud"}, asked)
    async with Client(server.mcp, elicitation_handler=handler) as client:
        result = await client.call_tool(
            "get_prompt", {"name": "kit_debug"}, raise_on_error=False
        )
        rendered = await client.call_tool(
            "get_prompt", {"name": "kit_debug", "arguments": {"app": "loki"}}
        )

    assert result.is_error
    assert result.content[0].text.startswith(REFUSAL)
    assert asked == []
    assert "Look at loki since 1h." in rendered.content[0].text
