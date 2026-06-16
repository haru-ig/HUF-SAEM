"""Main script to run the fuzzing process."""

import os
import time
from typing import Optional

import click
from rich.traceback import install

install()

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from Fuzz4All.make_target import make_target_with_config
from Fuzz4All.target.target import Target
from Fuzz4All.util.util import load_config_file


def is_ollama_model(model_name):
    return model_name.startswith("ollama/") or model_name in ["llama2", "starcoder"]


def write_to_file(fo, file_name):
    try:
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(fo)
    except:
        pass


def fuzz(
    target: Target,
    number_of_iterations: int,
    total_time: int,
    output_folder: str,
    resume: bool,
    otf: bool,
    coordinator=None,
):
    target.initialize()
    with Progress(
        TextColumn("Fuzzing • [progress.percentage]{task.percentage:>3.0f}%"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
    ) as p:
        task = p.add_task("Fuzzing", total=number_of_iterations)
        count = 0
        start_time = time.time()

        if resume:
            n_existing = [
                int(f.split(".")[0])
                for f in os.listdir(output_folder)
                if f.endswith(".fuzz")
            ]
            n_existing.sort(reverse=True)
            if len(n_existing) > 0:
                count = n_existing[0] + 1
            log = f" (resuming from {count})"
            p.console.print(log)

        p.update(task, advance=count)

        while (
            count < number_of_iterations
            and time.time() - start_time < total_time * 3600
        ):
            # Phase 3: cloze mode may override generation entirely
            cloze_code = coordinator.maybe_cloze_generate() if coordinator else None
            if cloze_code:
                fos = [cloze_code]
            else:
                fos = target.generate()
            if not fos:
                target.initialize()
                continue
            prev = []
            for index, fo in enumerate(fos):
                # Phase 2: optionally apply a synthesized mutator
                if coordinator:
                    fo = coordinator.maybe_apply_mutator(fo, count)
                file_name = os.path.join(output_folder, f"{count}.fuzz")
                write_to_file(fo, file_name)
                count += 1
                p.update(task, advance=1)
                # validation on the fly
                if otf:
                    f_result, message = target.validate_individual(file_name)
                    target.parse_validation_message(f_result, message, file_name)
                    prev.append((f_result, fo))
                    # Phase 3: record result for seed DB + cloze rate tracking
                    # Phase 4: collect coverage for SAFE programs
                    if coordinator:
                        coordinator.record_result_phase3(f_result, fo, count)
                        from Fuzz4All.target.target import FResult
                        if f_result == FResult.SAFE:
                            coordinator.run_coverage_and_update(file_name)
            target.update(prev=prev)
            # Phase 1: rotate to the next constraint spec when due
            if coordinator:
                coordinator.maybe_rotate_phase1_constraint(count)
            # Phase 4: periodically attempt constraint-solver injection
            if coordinator:
                solver_code = coordinator.maybe_run_constraint_solver(count)
                if solver_code:
                    solver_file = os.path.join(output_folder, f"{count}.fuzz")
                    write_to_file(solver_code, solver_file)
                    count += 1
                    p.update(task, advance=1)


# evaluate against the oracle to discover any potential bugs
# used after the generation
def evaluate_all(target: Target):
    target.validate_all()


@click.group()
@click.option(
    "config_file",
    "--config",
    type=str,
    default=None,
    help="Path to the configuration file.",
)
@click.pass_context
def cli(ctx, config_file):
    """Run the main using a configuration file."""
    if config_file is not None:
        config_dict = load_config_file(config_file)
        ctx.ensure_object(dict)
        ctx.obj["CONFIG_DICT"] = config_dict


@cli.command("main_with_config")
@click.pass_context
@click.option(
    "folder",
    "--folder",
    type=str,
    default="Results/test",
    help="folder to store results",
)
@click.option(
    "cpu",
    "--cpu",
    is_flag=True,
    help="to use cpu",  # this is for GPU resource low situations where only cpu is available
)
@click.option(
    "batch_size",
    "--batch_size",
    type=int,
    default=30,
    help="batch size for the model",
)
@click.option(
    "model_name",
    "--model_name",
    type=str,
    default="bigcode/starcoderbase",
    help="model to use",
)
@click.option(
    "target",
    "--target",
    type=str,
    default="",
    help="specific target to run",
)
def main_with_config(ctx, folder, cpu, batch_size, target, model_name):
    """Run the main using a configuration file."""
    config_dict = ctx.obj["CONFIG_DICT"]
    fuzzing = config_dict["fuzzing"]
    config_dict["fuzzing"]["output_folder"] = folder
    if cpu:
        config_dict["llm"]["device"] = "cpu"
    if batch_size:
        config_dict["llm"]["batch_size"] = batch_size
    if model_name != "":
        config_dict["llm"]["model_name"] = model_name
    if target != "":
        config_dict["fuzzing"]["target_name"] = target
    print(config_dict)

    target = make_target_with_config(config_dict)

    coordinator = None
    if "huf_saem" in config_dict:
        from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator
        coordinator = HUFSAEMCoordinator(config_dict, target)
        coordinator.initialize()

    if not fuzzing["evaluate"]:
        assert (
            not os.path.exists(folder) or fuzzing["resume"]
        ), f"{folder} already exists!"
        os.makedirs(fuzzing["output_folder"], exist_ok=True)
        fuzz(
            target=target,
            number_of_iterations=fuzzing["num"],
            total_time=fuzzing["total_time"],
            output_folder=folder,
            resume=fuzzing["resume"],
            otf=fuzzing["otf"],
            coordinator=coordinator,
        )
    else:
        evaluate_all(target)


if __name__ == "__main__":
    cli()
