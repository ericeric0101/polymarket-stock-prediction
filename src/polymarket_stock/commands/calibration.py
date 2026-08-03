"""Calibration command handlers."""

from __future__ import annotations

import json
from pathlib import Path

from ..buffer_sweep import buffer_values, run_buffer_sweep, walk_forward_buffer_sweep
from ..calibration import (
    calibrate_checkpoint_observations,
    calibrate_market_observations,
    calibrate_settled_positions,
    write_calibration_recommendation,
)
from ..probability_calibration import (
    sizing_readiness,
    stratified_first_signal_calibration,
    walk_forward_probability_calibration,
)
from ..top5_walk_forward import (
    parse_checkpoint_sets,
    parse_probability_values,
    top_five_policies,
    walk_forward_top_five_policy,
)
from .context import CommandContext
from .shared import _write_optional_json


def handle(context: CommandContext) -> None:
    arguments = context.arguments
    journal = context.journal
    if arguments.command == "calibrate-paper":
        recommendation = calibrate_settled_positions(journal.list_paper_positions())
        if arguments.write:
            write_calibration_recommendation(Path("data/model_calibration.json"), recommendation)
        print(json.dumps(recommendation.as_payload(), sort_keys=True))
    elif arguments.command == "calibrate-observations":
        print(
            json.dumps(calibrate_market_observations(journal.list_replay_observations()).as_payload(), sort_keys=True)
        )
    elif arguments.command == "calibrate-first-signals":
        observations = journal.list_first_signal_calibration_observations()
        report = {
            "calibration": stratified_first_signal_calibration(observations).as_payload(),
            "sizing_readiness": sizing_readiness(observations).as_payload(),
        }
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-probability-calibration":
        report = walk_forward_probability_calibration(
            journal.list_first_signal_calibration_observations(),
            training_days=arguments.training_days,
            validation_days=arguments.validation_days,
            minimum_training_samples=arguments.minimum_training_samples,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "calibrate-checkpoints":
        print(
            json.dumps(
                calibrate_checkpoint_observations(journal.list_checkpoint_observations()).as_payload(), sort_keys=True
            )
        )
    elif arguments.command == "buffer-sweep":
        report = run_buffer_sweep(
            journal.list_buffer_sweep_observations(),
            buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
            minimum_edge=arguments.minimum_edge,
            checkpoint_name=arguments.checkpoint,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-buffer-sweep":
        report = walk_forward_buffer_sweep(
            journal.list_buffer_sweep_observations(),
            buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
            minimum_edge=arguments.minimum_edge,
            checkpoint_name=arguments.checkpoint,
            training_days=arguments.training_days,
            validation_days=arguments.validation_days,
            minimum_training_trades=arguments.minimum_training_trades,
        ).as_payload()
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    elif arguments.command == "walk-forward-top-five":
        checkpoints = tuple(item.strip() for item in arguments.checkpoints.split(",") if item.strip())
        try:
            checkpoint_groups = parse_checkpoint_sets(arguments.checkpoint_sets, allowed=checkpoints)
            policies = top_five_policies(
                checkpoint_groups=checkpoint_groups,
                buffers=buffer_values(arguments.minimum_buffer, arguments.maximum_buffer, arguments.buffer_step),
                minimum_edges=parse_probability_values(arguments.minimum_edges),
                max_daily_entries=arguments.max_daily_entries,
                probability_calibration="RAW" if arguments.raw_probabilities else "TRAINING_BINNED_SHRINKAGE",
            )
            report = walk_forward_top_five_policy(
                journal.list_buffer_sweep_observations(),
                policies=policies,
                training_days=arguments.training_days,
                validation_days=arguments.validation_days,
                minimum_training_trades=arguments.minimum_training_trades,
            ).as_payload()
        except ValueError as error:
            raise SystemExit(f"walk-forward-top-five rejected arguments: {error}") from error
        _write_optional_json(arguments.output, report)
        print(json.dumps(report, sort_keys=True))
    else:
        raise AssertionError(f"Unexpected command for handler: {arguments.command}")
