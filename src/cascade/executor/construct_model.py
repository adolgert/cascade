import numpy as np

from cascade.core.log import getLoggers
from cascade.model import (
    Model, Var, SmoothGrid, Covariate
)

CODELOG, MATHLOG = getLoggers(__name__)


def rectangular_data_to_var(gridded_data):
    """Using this very regular data, where every age and time is present,
    construct an initial guess as a Var object. Very regular means that there
    is a complete set of ages-cross-times."""
    initial_ages = np.sort(np.unique(0.5 * (gridded_data.age_lower + gridded_data.age_upper)))
    initial_times = np.sort(np.unique(0.5 * (gridded_data.time_lower + gridded_data.time_upper)))

    guess = Var(ages=initial_ages, times=initial_times)
    for age, time in guess.age_time():
        found = gridded_data.query(
            "(age_lower <= @age) & (@age <= age_upper) & (time_lower <= @time) & (@time <= time_upper)")
        assert len(found) == 1, f"found {found}"
        guess[age, time] = float(found.iloc[0]["mean"])
    return guess


def const_value(value):

    def at_function(age, time):
        return value

    return at_function


def construct_model(data, local_settings, covariate_multipliers):
    ev_settings = local_settings.settings
    parent_location_id = local_settings.parent_location_id
    default_age_time = dict()
    default_age_time["age"] = np.linspace(0, 100, 21)
    default_age_time["time"] = np.linspace(1990, 2015, 6)
    for kind in ["age", "time"]:
        default_grid = getattr(ev_settings.model, f"default_{kind}_grid")
        if default_grid is not None:
            default_age_time[kind] = np.sort(np.array(default_grid, dtype=np.float))

    # Use this age and time when a smooth grid doesn't depend on age and time.
    single_age = default_age_time["age"][:1]
    single_time = [default_age_time["time"][len(default_age_time["time"]) // 2]]
    single_age_time = (single_age, single_time)

    nonzero_rates = [smooth.rate for smooth in ev_settings.rate]

    model = Model(
        nonzero_rates=nonzero_rates,
        parent_location=parent_location_id,
        child_location=list(data.locations.successors(parent_location_id)),
        covariates=covariates_list(covariate_multipliers),
        weights=None,
    )

    construct_model_rates(default_age_time, single_age_time, ev_settings, model)
    construct_model_random_effects(default_age_time, single_age_time, ev_settings, model)
    construct_model_covariates(default_age_time, single_age_time, covariate_multipliers, model)

    return model


def construct_model_rates(default_age_time, single_age_time, ev_settings, model):
    for smooth in ev_settings.rate:
        rate_grid = smooth_grid_from_smoothing_form(default_age_time, single_age_time, smooth)
        model.rate[smooth.rate] = rate_grid


def smooth_grid_from_smoothing_form(default_age_time, single_age_time, smooth):
    ages, times = construct_grid_ages_times(default_age_time, single_age_time, smooth)
    rate_grid = SmoothGrid(ages=ages, times=times)
    for kind in ["value", "dage", "dtime"]:
        if not smooth.default.is_field_unset(kind):
            getattr(rate_grid, kind)[:, :] = getattr(smooth.default, kind).prior_object
        else:
            pass  # An unset prior should be unused (dage for one age, dtime for one time)
    return rate_grid


def construct_grid_ages_times(default_age_time, single_age_time, smooth):
    if not smooth.is_field_unset("age_time_specific") and smooth.age_time_specific == 0:
        return single_age_time

    ages = smooth.age_grid
    if ages is None:
        # hasattr because this may be a Smoothing form or a Covariate form.
        if hasattr(smooth, "rate") and smooth.rate == "pini":
            ages = np.array([0], dtype=np.float)
        else:
            ages = default_age_time["age"]
    else:
        ages = np.sort(np.array(ages, dtype=np.float))
    times = smooth.time_grid
    if times is None:
        times = default_age_time["time"]
    else:
        times = np.sort(np.array(times, dtype=np.float))
    return ages, times


def construct_model_random_effects(default_age_time, single_age_time, ev_settings, model):
    """The settings may have random effects for many locations which aren't children
    of the current parent location. Only those random effects that apply to the children
    explicitly or to all locations (specified as location=None) are included."""
    if not ev_settings.random_effect:
        return

    for smooth in ev_settings.random_effect:
        re_grid = smooth_grid_from_smoothing_form(default_age_time, single_age_time, smooth)

        if not smooth.is_field_unset("location") and smooth.location in model.child_location:
            location = smooth.location
        else:
            # One smooth for all children when there isn't a child location.
            location = None
        model.random_effect[(smooth.rate, location)] = re_grid


def construct_model_covariates(default_age_time, single_age_time, covariate_multipliers, model):
    """The covariat multipliers are of all types: alpha, beta, and gamma. This adds
    their priors to the Model.

    Args:
        default_age_time (Tuple[ndarray,ndarray]): ages and times
        single_age_time (float, float): The single age and time to use if it's
            a point value.
        covariate_multipliers (List[EpiVizCovariateMultiplier): A list of specifications
            for covariate multipliers. This assumes data has already been read,
            because that data determines names for the multipliers.
    """
    for mulcov in covariate_multipliers:
        grid = smooth_grid_from_smoothing_form(default_age_time, single_age_time, mulcov.grid_spec)
        model[mulcov.group][mulcov.key] = grid


def covariates_list(covariate_multipliers):
    covariates = {mulcov.covariate for mulcov in covariate_multipliers}
    ordered = list(covariates)
    covariate_list = list()
    for c in ordered:
        covariate_list.append(Covariate(c.name, 0))
    return covariate_list
