"""Factorio Learning Environment (FLE) package."""

# Suppress slpp SyntaxWarning about invalid escape sequences
import importlib
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning, module="slpp")

__version__ = "0.4.3"

_LAZY_SUBMODULES = {"agents", "env", "eval", "cluster", "commons", "companion"}


def __getattr__(name: str):
    """Import large optional FLE surfaces only when they are actually used.

    The companion bridge deliberately has no dependency on the Docker, RCON,
    Lupa, or evaluation stacks.  Eagerly importing every FLE subpackage here
    made that lightweight entry point impossible on a normal Factorio install.
    """

    if name not in _LAZY_SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = importlib.import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_SUBMODULES)


# Preserve the package's historical import-time Gym registration whenever the
# optional environment stack is installed. Missing heavy dependencies remain a
# valid lightweight companion configuration.
try:
    from fle.env.gym_env.registry import register_all_environments

    register_all_environments()
except ImportError:
    pass


__all__ = ["agents", "env", "eval", "cluster", "commons", "companion"]
