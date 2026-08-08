from __future__ import annotations

import pytest

from polymarket_stock.commands.catalog import COMMAND_GROUPS, command_group
from polymarket_stock.commands.entrypoint import _HANDLERS, build_parser


def test_supervise_shadow_defaults_to_finnhub_only() -> None:
    arguments = build_parser().parse_args(["supervise-shadow"])

    assert arguments.spot_mode == "FINNHUB_ONLY"
    assert arguments.paper_entry_checkpoints == "1200_EDT"


def test_strategy_diagnostics_defaults_to_live_entry_checkpoint() -> None:
    arguments = build_parser().parse_args(["strategy-diagnostics"])

    assert arguments.checkpoint_names == "1200_EDT"


def test_every_catalog_command_has_a_parser_and_domain_handler() -> None:
    commands = tuple(command for group in COMMAND_GROUPS.values() for command in group)
    parser = build_parser()
    parsed_commands = set(parser._subparsers._group_actions[0].choices)

    assert len(commands) == len(set(commands))
    assert set(commands) == parsed_commands
    assert {command_group(command) for command in commands} == set(_HANDLERS)
    assert all(callable(_HANDLERS[command_group(command)]) for command in commands)


@pytest.mark.parametrize("command", tuple(command for group in COMMAND_GROUPS.values() for command in group))
def test_every_public_command_accepts_help(command: str) -> None:
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args([command, "--help"])

    assert result.value.code == 0
