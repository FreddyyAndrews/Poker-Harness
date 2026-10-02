"""The arena CLI documents itself: every command has a description,
examples and help for every option, every example parses, and the guide
covers every command."""
import argparse
import re
import shlex

import pytest

from poker_harness.cli.guide import SECTIONS, guide_text
from poker_harness.cli.helptext import commands
from poker_harness.cli.main import build_parser, main

PARSER = build_parser()
COMMANDS = commands(PARSER)
TOP = {p.split()[0] for p, _, _ in COMMANDS}
SKIP = ("[", "|", "(", "...", " / ", "COMMAND")
NUMBERS = {"K": "3", "N": "5"}            # placeholders where a number goes


@pytest.mark.parametrize("path, sub", [(p, s) for p, s, _ in COMMANDS], ids=[p for p, _, _ in COMMANDS])
def test_every_command_is_documented(path, sub):
    assert sub.description, f"arena {path}: no description"
    assert f"arena {path}" in (sub.epilog or ""), f"arena {path}: no examples"
    undocumented = [a.dest for a in sub._actions
                    if a.help is None and not isinstance(a, argparse._HelpAction)]
    assert not undocumented, f"arena {path}: options without help: {undocumented}"


@pytest.mark.parametrize("path", [p for p, _, _ in COMMANDS])
def test_every_help_page_renders(path, capsys):
    with pytest.raises(SystemExit) as e:
        main(path.split() + ["-h"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: arena {path}")


def example_lines(text):
    """Command lines in help text: `arena ...` at the start of a line or of a
    column (after 2+ spaces), not inside backticks or prose."""
    text = re.sub(r"\s*\\\n\s*", " ", text)     # join continued lines
    for m in re.finditer(r"(?:^|(?<=\s\s))(arena [^\n#`]*)(?!`)", text, re.MULTILINE):
        line = re.split(r"\s{2,}", m.group(1).strip())[0]
        if line.endswith("`") or any(s in line for s in SKIP):
            continue
        if line.split()[1:2] and line.split()[1] not in TOP:
            continue                                  # prose, e.g. "arena matches from ..."
        yield " ".join(NUMBERS.get(t, t) for t in line.split(" "))


def all_examples():
    out = [(f"arena {p} -h", line) for p, s, _ in COMMANDS for line in example_lines(s.epilog or "")]
    out += [("arena guide", line) for line in example_lines(guide_text())]
    out += [("arena -h", line) for line in example_lines(PARSER.description)]
    return sorted(set(out))


@pytest.mark.parametrize("where, line", all_examples(), ids=[l for _, l in all_examples()])
def test_every_example_parses(where, line, capsys):
    try:
        args = PARSER.parse_args(shlex.split(line)[1:])
    except SystemExit:
        pytest.fail(f"{where}: example doesn't parse: {line}\n{capsys.readouterr().err}")
    assert hasattr(args, "fn")


def test_there_are_plenty_of_examples():
    assert len(all_examples()) > 80


def test_guide_covers_every_top_level_command():
    top = {p.split()[0] for p, _, _ in COMMANDS}
    text = guide_text()
    missing = sorted(c for c in top if c not in text and c not in ("index", "guide"))
    assert not missing, f"arena guide doesn't mention: {missing}"


def test_guide_command(capsys):
    assert main(["guide"]) == 0
    assert "THE IMPROVEMENT LOOP" in capsys.readouterr().out
    for topic in SECTIONS:
        assert main(["guide", topic]) == 0
        assert capsys.readouterr().out == SECTIONS[topic]


def test_top_level_help_starts_with_the_guide(capsys):
    with pytest.raises(SystemExit):
        main(["-h"])
    out = capsys.readouterr().out
    assert "arena guide" in out[:400]                    # before the command list
    assert "spot.py" not in out


def test_stats_takes_the_bot_once():
    stats = dict((p, s) for p, s, _ in COMMANDS)["stats"]
    assert "--bot" not in {o for a in stats._actions for o in a.option_strings}
