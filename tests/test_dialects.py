"""Prompt dialects: three file formats, one MCP prompt.

The test that matters is the first one -- the same prompt written as this
server's own file, as a Claude command and as a Copilot ``.prompt.md``, each
producing one identical MCP prompt. Everything after it holds one rung of the
detection ladder or one substitution form, because those are the parts a
reader of the ladder cannot check by inspection.
"""

import logging

import pytest

from kubed.mcp_kb.catalogue.prompts import load_prompt, load_prompts

pytestmark = pytest.mark.unit

DIALECTS = ["mcp-kb", "claude", "copilot"]


def write(path, frontmatter, body):
    """One prompt file: a frontmatter block, then the body, as a source ships it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}")
    return path


def dropped(caplog):
    """The DEBUG lines about keys with no MCP meaning."""
    return [
        r.getMessage()
        for r in caplog.records
        if "no meaning over MCP" in r.getMessage()
    ]


# -- one prompt out of three files ---------------------------------------------

# (filename, frontmatter, body, the two argument descriptions, dialect)
OWN = (
    "investigate.md",
    "description: Investigate a service.\n"
    "arguments:\n"
    "- name: app\n"
    "  description: The workload.\n"
    "- name: since\n"
    "  description: How far back.\n",
    "Investigate {{ app }} over the last {{ since }}.",
    ["The workload.", "How far back."],
    "mcp-kb",
)
CLAUDE = (
    "investigate.md",
    "description: Investigate a service.\narguments: [app, since]\n",
    "Investigate $app over the last $since.",
    # Claude declares names and nothing about them, so there is no description
    # to publish -- asserted rather than papered over with the name.
    [None, None],
    "claude",
)
COPILOT = (
    "investigate.prompt.md",
    "description: Investigate a service.\n",
    "Investigate ${input:app:The workload.} over the last ${input:since:How far back.}.",
    ["The workload.", "How far back."],
    "copilot",
)


@pytest.mark.parametrize(
    ("filename", "frontmatter", "body", "descriptions", "dialect"),
    [OWN, CLAUDE, COPILOT],
    ids=DIALECTS,
)
def test_one_prompt_out_of_three_dialects(
    tmp_path, filename, frontmatter, body, descriptions, dialect
):
    prompt = load_prompt(write(tmp_path / filename, frontmatter, body), "lib")

    assert prompt.dialect == dialect
    assert prompt.name == "lib_investigate"
    assert prompt.description == "Investigate a service."
    assert [a.name for a in prompt.arguments] == ["app", "since"]
    assert [a.description for a in prompt.arguments] == descriptions
    assert not any(a.required for a in prompt.arguments)
    assert (
        prompt.render({"app": "api", "since": "1h"})
        == "Investigate api over the last 1h."
    )


# -- the detection ladder, rung by rung ----------------------------------------


def test_a_declared_dialect_beats_the_filename(tmp_path):
    """Rung 1: a plugin that says which dialect it ships is believed."""
    path = write(
        tmp_path / "x.prompt.md",
        "description: d\narguments:\n- name: app\n",
        "Hi {{ app }}.",
    )
    assert load_prompt(path, "lib", dialect="mcp-kb").dialect == "mcp-kb"


def test_the_prompt_md_suffix_beats_the_frontmatter_keys(tmp_path):
    """Rung 2: where the file is beats what its keys look like."""
    path = write(
        tmp_path / "x.prompt.md",
        "description: d\narguments:\n- name: app\n  description: The workload.\n",
        "Hi ${input:app}.",
    )
    prompt = load_prompt(path, "lib")
    assert prompt.dialect == "copilot"
    assert [a.name for a in prompt.arguments] == ["app"]
    assert prompt.render({"app": "you"}) == "Hi you."


def test_a_frontmatter_key_beats_the_body(tmp_path):
    """Rung 3: `agent:` is Copilot's, whatever the body's placeholders suggest."""
    path = write(
        tmp_path / "x.md", "description: d\nagent: copilot\n", "Do $ARGUMENTS."
    )
    prompt = load_prompt(path, "lib")
    assert prompt.dialect == "copilot"
    # Copilot has no `$ARGUMENTS`, so it is text like any other client-side form.
    assert prompt.render() == "Do $ARGUMENTS."


def test_an_instructions_file_is_not_a_prompt(tmp_path):
    """`applyTo` names files to apply guidance to: a rule, not a prompt."""
    path = write(
        tmp_path / "style.md", "description: d\napplyTo: '**/*.py'\n", "Type your code."
    )
    skipped = []
    assert load_prompts([path], library="lib", skipped=skipped) == []
    assert skipped == [(path, "not a prompt: has `applyTo`")]


def test_a_dialect_nothing_here_reads_is_skipped_with_its_name(tmp_path):
    """A config naming a dialect this server has no module for must skip the
    file, the way any other unreadable prompt does -- not take the harvest
    down with a ``KeyError`` nothing catches."""
    path = write(tmp_path / "x.md", "description: d\n", "Text.")
    skipped = []
    assert load_prompts([path], library="lib", dialect="gemini", skipped=skipped) == []
    assert skipped == [(path, "unknown dialect 'gemini'")]


def test_a_description_and_a_body_are_this_servers_own_dialect(tmp_path):
    """Rung 5: nothing to go on means the dialect every dialect agrees with."""
    path = write(tmp_path / "bare.md", "description: d\n", "Just text.")
    prompt = load_prompt(path, "lib")
    assert prompt.dialect == "mcp-kb"
    assert prompt.arguments == []
    assert prompt.render() == "Just text."


def test_arguments_cannot_be_names_and_objects_at_once(tmp_path):
    """Half a name list and half a list of objects is a file to refuse, not to guess."""
    path = write(
        tmp_path / "mixed.md",
        "description: d\narguments:\n- app\n- name: since\n",
        "Hi.",
    )
    with pytest.raises(ValueError, match="names or objects, not both"):
        load_prompt(path, "lib")


# -- Claude's substitution forms -----------------------------------------------


def test_claude_publishes_everything_typed_after_the_command(tmp_path):
    path = write(tmp_path / "fix.md", "description: d\n", "Fix $ARGUMENTS now.")
    prompt = load_prompt(path, "lib")
    assert prompt.dialect == "claude"
    assert [(a.name, a.description, a.required) for a in prompt.arguments] == [
        ("arguments", "Everything typed after the command.", False)
    ]
    assert prompt.render({"arguments": "the login bug"}) == "Fix the login bug now."


def test_claude_positionals_name_the_declared_arguments(tmp_path):
    path = write(
        tmp_path / "deploy.md",
        "description: d\narguments: app env\n",
        "Deploy $1 to $2.",
    )
    prompt = load_prompt(path, "lib")
    assert [a.name for a in prompt.arguments] == ["app", "env", "arguments"]
    assert prompt.render({"app": "api", "env": "prod"}) == "Deploy api to prod."
    # A client that fills one of two positionals gets a blank, not an error:
    # only this server's own dialect ever marks an argument required.
    assert prompt.render({"app": "api"}) == "Deploy api to ."


def test_claude_positionals_split_the_free_text_when_nothing_is_declared(tmp_path):
    path = write(tmp_path / "deploy.md", "description: d\n", "Deploy $1 to $2.")
    prompt = load_prompt(path, "lib")
    assert [a.name for a in prompt.arguments] == ["arguments"]
    assert prompt.render({"arguments": "api prod"}) == "Deploy api to prod."
    assert prompt.render({"arguments": "api"}) == "Deploy api to ."


def test_claude_indexes_arguments_the_same_way(tmp_path):
    path = write(tmp_path / "deploy.md", "description: d\n", "Deploy $ARGUMENTS[1].")
    prompt = load_prompt(path, "lib")
    assert prompt.render({"arguments": "api prod"}) == "Deploy api."


def test_claude_appends_the_free_text_the_body_never_names(tmp_path):
    """What Claude Code does with a command whose body ignores its arguments.

    Declared rather than detected: a body with no placeholders and frontmatter
    with no keys is the same file in every dialect, so what makes this one a
    command is the `commands/` tree it came from, which the caller knows.
    """
    path = write(tmp_path / "review.md", "description: d\n", "Review the diff.")
    prompt = load_prompt(path, "lib", dialect="claude")
    assert [a.description for a in prompt.arguments] == [
        "Everything typed after the command."
    ]
    assert (
        prompt.render({"arguments": "src/a.py"})
        == "Review the diff.\n\nARGUMENTS: src/a.py"
    )
    assert prompt.render() == "Review the diff."


def test_an_argument_hint_describes_the_free_text(tmp_path):
    """Penpot's shape: a description, a hint, and a body that says it in prose."""
    path = write(
        tmp_path / "penpot.md",
        "description: Work on a Penpot file.\nargument-hint: '[file] [board]'\n",
        "Open the file named in the arguments.",
    )
    prompt = load_prompt(path, "lib")
    assert prompt.dialect == "claude"
    assert [(a.name, a.description, a.required) for a in prompt.arguments] == [
        ("arguments", "[file] [board]", False)
    ]
    assert (
        prompt.render({"arguments": "Homelab Dashboard"})
        == "Open the file named in the arguments.\n\nARGUMENTS: Homelab Dashboard"
    )


def test_a_dollar_amount_is_not_a_positional(tmp_path):
    path = write(
        tmp_path / "bill.md", "description: d\n", "Charge $5.00 for $ARGUMENTS."
    )
    prompt = load_prompt(path, "lib")
    assert prompt.render({"arguments": "the job"}) == "Charge $5.00 for the job."


def test_an_undeclared_name_is_left_as_written(tmp_path):
    """Only a declared name is a placeholder; `$dir` in prose is prose."""
    path = write(
        tmp_path / "walk.md",
        "description: d\narguments: [app]\n",
        "Walk $dir for $app.",
    )
    prompt = load_prompt(path, "lib")
    assert prompt.render({"app": "api"}) == "Walk $dir for api."


# -- Copilot's inputs ----------------------------------------------------------


def test_copilot_inputs_collapse_by_name_and_keep_the_first_placeholder(tmp_path):
    path = write(
        tmp_path / "log.prompt.md",
        "description: d\n",
        "${input:app:The workload.} then ${input:app} and ${input:since}.",
    )
    prompt = load_prompt(path, "lib")
    assert [(a.name, a.description) for a in prompt.arguments] == [
        ("app", "The workload."),
        ("since", None),
    ]
    assert prompt.render({"app": "api"}) == "api then api and ."


def test_copilot_appends_the_free_text_of_an_argument_hint(tmp_path):
    path = write(
        tmp_path / "review.prompt.md",
        "description: d\nargument-hint: '[file]'\n",
        "Review the diff.",
    )
    prompt = load_prompt(path, "lib")
    assert [(a.name, a.description) for a in prompt.arguments] == [
        ("arguments", "[file]")
    ]
    assert prompt.render({"arguments": "src/a.py"}) == "Review the diff.\n\nsrc/a.py"


# -- what every dialect shares -------------------------------------------------


def test_only_this_servers_own_dialect_carries_a_title(tmp_path):
    own = write(
        tmp_path / "t.md", "description: d\ntitle: Investigate a service\n", "Text."
    )
    assert load_prompt(own, "lib").title == "Investigate a service"
    command = write(
        tmp_path / "c.md", "description: d\ntitle: t\narguments: [app]\n", "Hi $app."
    )
    assert load_prompt(command, "lib").title is None


@pytest.mark.parametrize("dialect", DIALECTS)
def test_nothing_in_a_body_is_ever_executed(tmp_path, dialect):
    """A `!` line and a `!{…}` block are text: a prompt server that ran shell
    from a repository it fetched would be a supply-chain hole."""
    body = "!`rm -rf /`\n\n!{echo hi}\n"
    path = write(tmp_path / f"{dialect}.md", "description: d\n", body)
    assert load_prompt(path, "lib", dialect=dialect).render() == body


@pytest.mark.parametrize("dialect", DIALECTS)
def test_client_side_placeholders_render_verbatim(tmp_path, dialect):
    """They name things only the client has, so substituting them would be a lie."""
    body = "${selection} #file:x.py @src/a.py ${CLAUDE_PROJECT_DIR} ${file}\n"
    path = write(tmp_path / f"{dialect}.md", "description: d\n", body)
    assert load_prompt(path, "lib", dialect=dialect).render() == body


def test_keys_with_no_mcp_meaning_are_logged_once(tmp_path, caplog):
    path = write(
        tmp_path / "commit.md",
        "description: d\nallowed-tools: Bash(git*)\nmodel: opus\narguments: [message]\n",
        "Commit $message.",
    )
    with caplog.at_level(logging.DEBUG, logger="kubed.mcp_kb.catalogue.prompts"):
        prompt = load_prompt(path, "lib")
    assert len(dropped(caplog)) == 1
    assert "allowed-tools" in dropped(caplog)[0]
    assert "model" in dropped(caplog)[0]
    # The keys that do mean something are still read, and nothing is appended
    # for a body that already names its argument.
    assert [a.name for a in prompt.arguments] == ["message"]
    assert prompt.render({"message": "fix it"}) == "Commit fix it."


def test_a_file_with_nothing_to_drop_says_nothing(tmp_path, caplog):
    path = write(tmp_path / "plain.md", "description: d\n", "Text.")
    with caplog.at_level(logging.DEBUG, logger="kubed.mcp_kb.catalogue.prompts"):
        load_prompt(path, "lib")
    assert dropped(caplog) == []
