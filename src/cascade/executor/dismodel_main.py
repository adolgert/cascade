import json
import pickle
from argparse import ArgumentParser
from pathlib import Path
from textwrap import fill

import networkx as nx

from cascade.core import getLoggers
from cascade.executor.execution_context import make_execution_context
from cascade.executor.job_definitions import job_graph_from_settings
from cascade.input_data.db.configuration import load_settings
from cascade.input_data.db.locations import location_hierarchy
from cascade.runner.entry import entry
from cascade.input_data.db.configuration import json_settings_to_frozen_settings

CODELOG, MATHLOG = getLoggers(__name__)


class Application:
    """
    Responsible for management of settings and creation of job graphs.

    All arguments have default None, which is the typical way to
    instantiate this, unless it is under test. Using arguments here
    makes it unnecessary to use ``create_settings`` or ``load_settings``.

    Args:
        locations (nx.DiGraph): Graph of locations.
        settings (SimpleNamespace): Settings for the whole run.
        execution_context (ExecutionContext): defines the environment.
    """
    def __init__(self, locations=None, settings=None, execution_context=None):
        self.locations = locations
        self.settings = settings
        self.execution_context = execution_context

    def add_arguments(self, parser=None):
        """Add arguments to an argument parser. These arguments are relevant
        to the application but not to how it is run.

        Args:
            parser (ArgumentParser): If not supplied, a parser is created.

        Returns:
            ArgumentParser: The one that is created, or the one passed in.
        """
        if parser is None:
            parser = ArgumentParser()
        parser.add_argument(
            "--meid", type=int,
            help="Modelable entity ID. This identifies the cause of disease.",
        )
        parser.add_argument(
            "--mvid", type=int,
            help=("Model version ID. There are multiple model versions for "
                  "each model entity"),
        )
        context_parser = parser.add_argument_group(
            "context",
            "Settings to change the process directories and environment."
        )
        context_parser.add_argument(
            "--infrastructure", action="store_true",
            help=fill("Whether we are running as infrastructure component. "
                      "Controls whether this tries to store files in the "
                      "base directory or whether it works in the"
                      "current directory."),
        )
        context_parser.add_argument(
            "--base-directory", type=Path, default=".",
            help="Directory in which to find and store files.",
        )

        pdb_parser = parser.add_argument_group(
            "debug",
            "These affect how this executes, for debugging."
        )
        pdb_parser.add_argument(
            "--no-upload", action="store_true",
            help=fill("This turns off all writing to databases, so that a run "
                      "will not affect outputs"),
        )
        pdb_parser.add_argument(
            "--db-only", action="store_true",
            help=fill("Run until it creates a Dismod-AT db file, and then quit. "
                      "This may make less sense when talking about a large "
                      "graph of computations."),
        )

        data_parser = parser.add_argument_group(
            "data",
            "Parsers to change what data is used for input."
        )
        data_parser.add_argument(
            "--settings-file", type=Path,
            help="Read settings from this file.",
        )
        data_parser.add_argument(
            "-b", "--bundle-file", type=Path,
            help=fill("The bundle normally comes from the databases but this "
                      "lets you specify a particular file as input. If this "
                      "isn't given, it will look for the bundle in the db or "
                      "in a known input directory."),
        )
        data_parser.add_argument(
            "-s", "--bundle-study-covariates-file", type=Path,
            help=fill("Read the study covariates from a separate file. "
                      "If this is not specified, then they are read from "
                      "either the databases or from the known file location."),
        )

        graph_parser = parser.add_argument_group(
            "graph",
            "Changes to the graph of work that is done."
        )
        graph_parser.add_argument(
            "--skip-cache", action="store_true",
            help=fill("Don't save bundle data to tier 3. Instead, read "
                      "it directly from tier 2."),
        )
        graph_parser.add_argument("--num-samples", type=int, help="Override number of samples.")

        sub_graph = parser.add_argument_group(
            "sub_graph",
            "These arguments select a subset of nodes to run."
        )
        sub_graph.add_argument("--location-id", type=int, help="location ID for this work")
        sub_graph.add_argument("--sex", type=str, help="sex as male, female, both")
        sub_graph.add_argument("--recipe", type=str, help="name of the recipe")
        sub_graph.add_argument("--name", type=str, help="job within the recipe")
        return parser

    def create_settings(self, args):
        # We need a sort-of-correct execution context when we first load
        # and then it gets refined after settings are loaded.
        execution_context = execution_context_without_settings(args)
        self.settings = load_settings(
            execution_context, args.meid, args.mvid, args.settings_file)
        self.locations = location_hierarchy(
            location_set_version_id=self.settings.location_set_version_id,
            gbd_round_id=self.settings.gbd_round_id
        )
        configure_execution_context_from_settings(
            execution_context, self.settings
        )
        self.execution_context = execution_context

    def load_settings(self, args):
        # The execution context isn't part of the settings, so it is
        # rebuilt here when settings are loaded.
        self.execution_context = execution_context_without_settings(args)
        base = self.execution_context.model_base_directory(0)
        setting_file = base / "settings.json"
        settings_dict = json.load(setting_file.open("r"))
        self.settings = json_settings_to_frozen_settings(settings_dict)
        location_file = base / "locations.pickle"
        self.locations = pickle.load(location_file.open("rb"))
        CODELOG.info(f"Loading settings from {setting_file} and "
                     f"locations from {location_file}")
        configure_execution_context_from_settings(
            self.execution_context, self.settings
        )

    def save_settings(self):
        base = self.execution_context.model_base_directory(0)
        base.mkdir(exist_ok=True, parents=True)
        setting_file = base / "settings.json"
        json.dump(self.settings.to_dict(), setting_file.open("w"), indent=4)
        location_file = base / "locations.pickle"
        pickle.dump(self.locations, location_file.open("wb"))
        CODELOG.info(f"Saving settings to {setting_file} "
                     f"and locations to {location_file}")

    def graph_of_jobs(self, args):
        return job_graph_from_settings(self.locations, self.settings, args)

    def sub_graph_to_run(self, args):
        job_graph = self.graph_of_jobs(args)
        nodes = job_graph.nodes

        for search in ["location_id", "recipe", "sex", "name"]:
            if search in args:
                nodes = [n for n in nodes if getattr(n, search) == getattr(args, search)]

        sub_graph = nx.subgraph(job_graph, nodes)
        sub_graph.graph["execution_context"] = self.execution_context
        return sub_graph


def execution_context_without_settings(args):
    execution_context = make_execution_context(
        gbd_round_id=6
    )
    if args.infrastructure:
        execution_context.parameters.organizational_mode = "infrastructure"
    else:
        execution_context.parameters.organizational_mode = "local"

    execution_context.parameters.base_directory = args.base_directory

    if args.meid:
        execution_context.parameters.modelable_entity_id = args.meid
    if args.mvid:
        execution_context.parameters.model_version_id = args.mvid

    return execution_context


def configure_execution_context_from_settings(execution_context, settings):
    """
    This later configuration exists because the application is
    started by telling it the model version ID but *not telling it the
    modelable entity ID.* That modelable entity ID is used to decide
    where files are on disk, so we need it early.
    """
    for param in ["modelable_entity_id", "model_version_id"]:
        setattr(execution_context.parameters, param, getattr(settings.model, param))


if __name__ == "__main__":
    app = Application()
    entry(app)
