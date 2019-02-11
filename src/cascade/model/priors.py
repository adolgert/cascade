from copy import copy
from functools import total_ordering

import numpy as np
from scipy.stats import norm, laplace, t

from cascade.core import getLoggers
CODELOG, MATHLOG = getLoggers(__name__)

# A description of how dismod interprets these distributions and their parameters can be found here:
# https://bradbell.github.io/dismod_at/doc/prior_table.htm


class PriorError(Exception):
    pass


@total_ordering
class _Prior:
    """The base for all Priors
    """

    density = None

    def __init__(self, name=None):
        self.name = name

    def _parameters(self):
        raise NotImplementedError()

    def parameters(self):
        return dict(density=self.density, **self._parameters())

    def assign(self, **kwargs):
        """Create a new distribution with modified parameters."""
        modified = copy(self)
        if set(kwargs.keys()) - set(self.__dict__.keys()):
            missing = list(sorted(set(kwargs.keys()) - set(self.__dict__.keys())))
            raise AttributeError(f"The prior doesn't have these attributes {missing}.")
        modified.__dict__.update(kwargs)
        return modified

    def __hash__(self):
        return hash((frozenset(self.parameters().items()), self.name))

    def __eq__(self, other):
        if not isinstance(other, _Prior):
            return NotImplemented
        return self.name == other.name and self.parameters() == other.parameters()

    def __lt__(self, other):
        if not isinstance(other, _Prior):
            return NotImplemented
        self_dict = sorted([(k, v) for k, v in dict(name=self.name, **self.parameters()).items() if v is not None])
        other_dict = sorted([(k, v) for k, v in dict(name=other.name, **other.parameters()).items() if v is not None])

        return self_dict < other_dict

    def __repr__(self):
        return f"<{type(self).__name__} {self.parameters()}>"


def _validate_bounds(lower, mean, upper):
    any_nones = lower is None or mean is None or upper is None
    any_invalid = any_nones or np.isnan(lower) or np.isnan(mean) or np.isnan(upper)
    if any_invalid:
        raise PriorError(f"Bounds contain invalid values: lower={lower} mean={mean} upper={upper}")
    if not lower <= mean <= upper:
        raise PriorError(f"Bounds are inconsistent: lower={lower} mean={mean} upper={upper}")


def _validate_standard_deviation(standard_deviation):
    if standard_deviation is None or np.isnan(standard_deviation) or standard_deviation < 0:
        raise PriorError(f"Standard deviation must be positive: standard deviation={standard_deviation}")


def _validate_nu(nu):
    if nu is None or np.isnan(nu) or nu < 0:
        raise PriorError(f"Nu must be positive: nu={nu}")


class Uniform(_Prior):
    density = "uniform"

    def __init__(self, lower, upper, mean=None, eta=None, name=None):
        super().__init__(name=name)
        if mean is None:
            mean = (upper + lower) / 2
        _validate_bounds(lower, mean, upper)

        self.lower = lower
        self.upper = upper
        self.mean = mean
        self.eta = eta

    def mle(self, draws):
        """Using draws, assign a new mean, guaranteed between lower and upper."""
        return self.assign(mean=min(self.upper, max(self.lower, np.mean(draws))))

    def _parameters(self):
        return {"lower": self.lower, "upper": self.upper, "mean": self.mean, "eta": self.eta}


class Constant(_Prior):
    density = "uniform"

    def __init__(self, value, name=None):
        super().__init__(name=name)
        self.value = value

    def mle(self, _):
        """Don't change the const value."""
        return copy(self)

    def _parameters(self):
        return {"lower": self.value, "upper": self.value, "mean": self.value}


class Gaussian(_Prior):
    r"""A Gaussian is

    .. math::

       f(x) = \frac{1}{2\pi \sigma^2} e^{-(x-\mu)^2/(2\sigma^2)}

    where :math:`\sigma` is the variance and :math:`\mu` the mean.

    Args:
        mean (float): This is :math:`\mu`.
        standard_deviation (float): This is :math:`\sigma`.
        lower (float): lower limit.
        upper (float): upper limit.
        eta (float): Offset for calculating standard deviation.
        name (str): Name for this prior.
    """
    density = "gaussian"

    def __init__(self, mean, standard_deviation, lower=float("-inf"), upper=float("inf"), eta=None, name=None):
        super().__init__(name=name)
        _validate_bounds(lower, mean, upper)
        _validate_standard_deviation(standard_deviation)

        self.lower = lower
        self.upper = upper
        self.mean = mean
        self.standard_deviation = standard_deviation
        self.eta = eta

    def mle(self, draws):
        """Assign new mean and stdev, with mean clamped between
        upper and lower."""
        # The mean and standard deviation for Dismod-AT match the location
        # and scale used by Scipy.
        mean, std = norm.fit(draws)
        return self.assign(
            mean=min(self.upper, max(self.lower, mean)),
            standard_deviation=std
        )

    def _parameters(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "mean": self.mean,
            "std": self.standard_deviation,
            "eta": self.eta,
        }


class Laplace(Gaussian):
    r"""
    This version of the Laplace distribution is parametrized by its variance
    instead of by scaling of the axis. Usually, the Laplace distribution is

    .. math::

        f(x) = \frac{1}{2b}e^{-|x-\mu|/b}

    where :math:`\mu` is the mean and :math:`b` is the scale, but the
    variance is :math:`\sigma^2=2b^2`, so the Dismod-AT version looks like

    .. math::

        f(x) = \frac{1}{\sqrt{2\pi\sigma^2}e^{-\sqrt{2}|x-\mu|/\sigma}.

    The standard deviation assigned is :math:`\sigma`.
    """
    density = "laplace"

    def mle(self, draws):
        """Assign new mean and stdev, with mean clamped between
        upper and lower."""
        mean, scale = laplace.fit(draws)
        return self.assign(
            mean=min(self.upper, max(self.lower, mean)),
            standard_deviation=scale * np.sqrt(2)  # This is the adjustment.
        )


class StudentsT(_Prior):
    r"""
    This Students-t must have :math:`\nu>2`.
    Students-t distribution is usually

    .. math::

        f(x,\nu) = \frac{\Gamma((\nu+1)/2)}{\sqrt{\pi\nu}\Gamma(\nu)}(1+x^2/\nu)^{-(\nu+1)/2}

    with mean 0 for :math:`\nu>1`. The variance is :math:`\nu/(\nu-2)` for
    :math:`\nu>2`. Dismod-AT rewrites this using :math:`\sigma^2=\nu/(\nu-2)`
    to get

    .. math::

        f(x) = \frac{\Gamma((\nu+1)/2)}{\sqrt(\pi\nu)\Gamma(\nu/2)}
               \left(1 + (x-\mu)^2/(\sigma^2(\nu-2))\right)^{-(\nu+1)/2}


    """
    density = "students"

    def __init__(self, mean, standard_deviation, nu, lower=float("-inf"), upper=float("inf"), eta=None, name=None):
        super().__init__(name=name)
        _validate_bounds(lower, mean, upper)
        _validate_standard_deviation(standard_deviation)
        _validate_nu(nu)

        self.lower = lower
        self.upper = upper
        self.mean = mean
        self.standard_deviation = standard_deviation
        self.nu = nu
        self.eta = eta

    def mle(self, draws):
        """Assign new mean and stdev, with mean clamped between
        upper and lower."""
        # This fixes the nu value.
        nu, mean, scale = t.fit(draws, fix_df=self.nu)
        return self.assign(
            mean=min(self.upper, max(self.lower, mean)),
            standard_deviation=scale * np.sqrt(nu / (nu - 2))
        )

    def _parameters(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "mean": self.mean,
            "std": self.standard_deviation,
            "nu": self.nu,
            "eta": self.eta,
        }


class LogGaussian(_Prior):
    r"""
    Dismod-AT parametrizes the Log-Gaussian with the standard deviation
    as

    .. math::

        f(x) = \frac{1}{\sqrt{2\pi\sigma^2}} e^{-\log((x-\mu)/\sigma)^2/2}

    """
    density = "log_gaussian"

    def __init__(self, mean, standard_deviation, eta, lower=float("-inf"), upper=float("inf"), name=None):
        super().__init__(name=name)
        _validate_bounds(lower, mean, upper)
        _validate_standard_deviation(standard_deviation)

        self.lower = lower
        self.upper = upper
        self.mean = mean
        self.standard_deviation = standard_deviation
        self.eta = eta

    def mle(self, draws):
        """Assign new mean and stdev, with mean clamped between
        upper and lower."""
        # XXX not using MLE. Need to work out math.
        return self.assign(
            mean=min(self.upper, max(self.lower, np.mean(draws))),
            standard_deviation=np.std(draws)
        )

    def _parameters(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "mean": self.mean,
            "std": self.standard_deviation,
            "eta": self.eta,
        }


class LogLaplace(LogGaussian):
    density = "log_laplace"


class LogStudentsT(_Prior):
    density = "log_students"

    def __init__(self, mean, standard_deviation, nu, eta, lower=float("-inf"), upper=float("inf"), name=None):
        super().__init__(name=name)
        _validate_bounds(lower, mean, upper)
        _validate_standard_deviation(standard_deviation)
        _validate_nu(nu)

        self.lower = lower
        self.upper = upper
        self.mean = mean
        self.standard_deviation = standard_deviation
        self.nu = nu
        self.eta = eta

    def mle(self, draws):
        """Assign new mean and stdev, with mean clamped between
        upper and lower."""
        # XXX not using MLE. Need to work out math.
        return self.assign(
            mean=min(self.upper, max(self.lower, np.mean(draws))),
            standard_deviation=np.std(draws)
        )

    def _parameters(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "mean": self.mean,
            "std": self.standard_deviation,
            "nu": self.nu,
            "eta": self.eta,
        }


# Useful predefined priors

NO_PRIOR = Uniform(float("-inf"), float("inf"), 0, name="null_prior")
ZERO = Uniform(0, 0, 0, name="constrain_to_zero")
ZERO_TO_ONE = Uniform(0, 1, 0.1, name="uniform_zero_to_one")
MINUS_ONE_TO_ONE = Uniform(-1, 1, 0, name="uniform_negative_one_to_one")


DENSITY_ID_TO_PRIOR = {
    0: Uniform,
    1: Gaussian,
    2: Laplace,
    3: StudentsT,
    4: LogGaussian,
    5: LogLaplace,
    6: LogStudentsT,
}


def prior_distribution(parameters):
    density, lower, upper, value, stdev, eta, nu = [
        parameters[name] for name in
        [
            "density", "lower", "upper", "mean", "std", "eta", "nu"
        ]
    ]
    if np.isclose(lower, upper):
        return Constant(value)
    elif density == "uniform":
        return Uniform(lower, upper, value, eta)
    elif density == "gaussian":
        return Gaussian(value, stdev, lower, upper, eta)
    elif density == "laplace":
        return Laplace(value, stdev, lower, upper, eta)
    elif density == "students":
        return StudentsT(value, stdev, nu, lower, upper, eta)
    elif density == "log_gaussian":
        return LogGaussian(value, stdev, eta, lower, upper)
    elif density == "log_laplace":
        return LogLaplace(value, stdev, eta, lower, upper)
    elif density == "log_students":
        return LogStudentsT(value, stdev, nu, eta, lower, upper)
    else:
        return None
