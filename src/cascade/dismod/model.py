from cascade.dismod.constants import WeightEnum
from cascade.dismod.dismod_groups import DismodGroups
from cascade.dismod.var import Var
from cascade.dismod.smooth_grid import smooth_grid_from_var


class Model(DismodGroups):
    """
    A DismodGroups container of SmoothGrid.

    Uses locations as given and translates them into nodes for Dismod-AT.
    Uses ages and times as given and translates them into ``age_id``
    and ``time_id`` for Dismod-AT.
    """
    def __init__(self, nonzero_rates, parent_location, child_location, weights=None):
        """
        >>> locations = location_hierarchy(execution_context)
        >>> m = Model(["chi", "omega", "iota"], 6, locations)
        """
        super().__init__()
        self.nonzero_rates = nonzero_rates
        self.location_id = parent_location
        self.child_location = child_location
        self.covariates = list()  # of class Covariate
        # There are always four weights, constant, susceptible,
        # with_condition, and total.
        if weights:
            self.weights = weights
        else:
            self.weights = dict()

    def write(self, writer):
        self._ensure_weights()
        writer.start_model(self.nonzero_rates, self.child_location)
        for group in self.values():
            for grid in group.values():
                writer.write_ages_and_times(grid.ages, grid.times)
        for weight_value in self.weights.values():
            writer.write_ages_and_times(weight_value.ages, weight_value.times)

        writer.write_covariate(self.covariates)
        writer.write_weights(self.weights)
        for group_name, group in self.items():
            if group_name == "rate":
                for rate_name, grid in group.items():
                    writer.write_rate(rate_name, grid)
            elif group_name == "random_effect":
                for (covariate, rate_name), grid in group.items():
                    writer.write_random_effect(covariate, rate_name, grid)
            elif group_name in {"alpha", "beta", "gamma"}:
                for (covariate, target), grid in group.items():
                    writer.write_mulcov(group_name, covariate, target, grid)
            else:
                raise RuntimeError(f"Unknown kind of field {group_name}")

    def _ensure_weights(self):
        """If weights weren't made, then make them constant. Must be done after
        there is data in the Model."""
        # Find an age and time already in the model because adding an
        # age and time outside the model can change the integration ranges.
        arbitrary_grid = next(iter(self.rate.values()))
        arbitrary_age_time = arbitrary_grid.age_time
        one_age_time = (arbitrary_age_time[0][0:1], arbitrary_age_time[1][0:1])

        for kind in (weight.name for weight in WeightEnum):
            if kind not in self.weights:
                self.weights[kind] = Var(one_age_time)
                self.weights[kind].grid.loc[:, "mean"] = 1.0


def model_from_vars(vars, parent_location, weights=None):
    """
    Given values across all rates, construct a model with loose priors
    in order to be able to predict from those rates.

    Args:
        vars (DismodGroups[Var]): Values on grids.
        parent_location (int): A parent location, because that isn't
            in the keys.
        weights (Dict[Var]): Population weights for integrands.

    Returns:
        Model: with Uniform distributions everywhere and no mulstd.
    """
    child_locations = [k[1] for k in vars.random_effect.keys()]
    nonzero_rates = list(vars.rate.keys())
    model = Model(nonzero_rates, parent_location, child_locations, weights)

    # Maybe there is something special for handling random effects.
    strictly_positive = dict()
    strictly_positive["rate"] = True
    for group_name, group in vars.items():
        for key, var in group.items():
            must_be_positive = strictly_positive.get(group_name, False)
            model[group_name][key] = smooth_grid_from_var(var, must_be_positive)

    return model
