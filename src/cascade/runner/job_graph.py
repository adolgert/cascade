import networkx as nx

import gridengineapp
from cascade.core import getLoggers

CODELOG, MATHLOG = getLoggers(__name__)


class RecipeIdentifier:
    """This is a tuple that identifies a recipe within the graph of recipes.
    A Recipe is the set of steps executed for a single location, but it is
    also the set of steps run at the start or end of a whole model. For
    instance, aggregation is a recipe.

    1. Location ID, which may be 0, to indicate that this task is
       associated with no location, or all locations.
    2. A string identifier for the set of tasks at this location.

    Args:
        location_id (int): Location identifier from GBD.
        recipe (str): Identifies a list of tasks to do.
        sex (str): One of male, female, or both, to indicate sex split.
    """
    __slots__ = ["_location_id", "_recipe", "_sex"]

    def __init__(self, location_id, recipe, sex):
        assert isinstance(location_id, int)
        assert isinstance(recipe, str)
        allowed_sexes = {"male", "female", "both"}
        assert sex in allowed_sexes
        assert recipe not in allowed_sexes

        self._location_id = location_id
        self._recipe = recipe
        self._sex = sex

    @property
    def location_id(self):
        return self._location_id

    @property
    def recipe(self):
        return self._recipe

    @property
    def sex(self):
        return self._sex

    def __eq__(self, other):
        if not isinstance(other, RecipeIdentifier):
            return False
        return all(getattr(self, x) == getattr(other, x)
                   for x in ["location_id", "recipe", "sex"])

    def __hash__(self):
        return hash((self._location_id, self._recipe, self._sex))

    def __repr__(self):
        return f"RecipeIdentifier({self.location_id}, {self.recipe}, {self.sex})"


class JobIdentifier(RecipeIdentifier):
    __slots__ = ["_location_id", "_recipe", "_sex", "_name"]

    def __init__(self, recipe_identifier, name):
        self._name = name
        super().__init__(
            recipe_identifier.location_id,
            recipe_identifier.recipe,
            recipe_identifier.sex,
        )

    @property
    def name(self):
        return self._name

    def __eq__(self, other):
        return RecipeIdentifier.__eq__(self, other) and self.name == other.name

    def __hash__(self):
        return hash((self._location_id, self._recipe, self._sex, self.name))

    def __repr__(self):
        return f"RecipeIdentifier({self.location_id}, {self.recipe}, {self.sex}, {self.name})"

    def __str__(self):
        return f"{self.location_id}_{self.recipe}_{self.sex}_{self.name}"

    @property
    def arguments(self):
        """The command-line arguments that would select this job."""
        return [str(x) for x in [
            "--location-id", self.location_id, "--recipe", self.recipe,
            "--sex", self.sex, "--name", self.name
        ]]


class CascadeJob(gridengineapp.Job):
    """
    Responsible for ensuring a thread of execution can complete.
    It handles:

     * Declaring what resources it needs from memory and cores.
     * Using file descriptors to describe external input and output.

    Each job describes one or more *tasks,* where the job's
    multiplicity is the number of tasks to start for this job.
    This corresponds to Grid Engine jobs and tasks.
    """
    def __init__(self, name, recipe_identifier, local_settings):
        self.name = name
        self.recipe = recipe_identifier
        self.local_settings = local_settings
        self.inputs = dict()
        self.outputs = dict()
        if name != "compute_draws_from_parent_fit":
            self.multiplicity = 1
        else:
            self.multiplicity = local_settings.number_of_fixed_effect_samples

    def __call__(self, *args, **kwargs):
        CODELOG.info(f"Running {self.job_identifier}")

    @property
    def job_identifier(self):
        return JobIdentifier(self.recipe, self.name)

    @property
    def memory_resource(self):
        """The memory is a float and declared in Gigabytes."""
        return 16

    @property
    def thread_resource(self):
        """The thread is a thread of execution and an integer."""
        return 2

    def mock_run(self, execution_context, check_inputs=True):
        if check_inputs:
            missing = self.input_missing(execution_context)
            if missing:
                CODELOG.info(f"missing inputs {missing} for {self}")
                raise RuntimeError(missing)
        for output in self.outputs.values():
            output.mock(execution_context)

    def input_missing(self, execution_context):
        return self.entity_missing(execution_context, self.inputs)

    def output_missing(self, execution_context):
        return self.entity_missing(execution_context, self.outputs)

    def entity_missing(self, execution_context, inputs_outputs):
        not_ready = dict()
        for name, entity in inputs_outputs.items():
            validation = entity.validate(execution_context)
            if validation is not None:
                not_ready[name] = validation
        return not_ready

    def __repr__(self):
        return "Job(" + ", ".join(str(x) for x in [
            self.name, self.recipe
        ]) + ")"


def recipe_graph_to_job_graph(recipe_graph):
    """Each node in the recipe graph contains a list of jobs. This creates
    a graph of the relationships among the jobs, assuming each list of jobs
    must be executed in order, through the list."""
    recipe_edges = dict()  # recipe_identifier -> (input node, output node)
    job_graph = nx.DiGraph()
    for copy_identifier in recipe_graph.nodes:
        job_list = recipe_graph.node[copy_identifier]["job_list"]
        if len(job_list) < 1:
            raise RuntimeError(f"Recipe {copy_identifier} doesn't have any sub-jobs.")
        job_ids = [job_node.job_identifier for job_node in job_list]
        job_graph.add_nodes_from(
            (jid, dict(job=add_job))
            for (jid, add_job) in zip(job_ids, job_list)
        )
        job_graph.add_edges_from(zip(job_ids[:-1], job_ids[1:]))
        recipe_edges[copy_identifier] = dict(input=job_ids[0], output=job_ids[-1])

        if copy_identifier == recipe_graph.graph["root"]:
            job_graph.graph["root"] = job_ids[0]

    assert "root" in job_graph.graph, "Could not find a root node for the graph"
    job_graph.add_edges_from([
        (recipe_edges[start]["output"], recipe_edges[finish]["input"])
        for (start, finish) in recipe_graph.edges
    ])
    return job_graph
