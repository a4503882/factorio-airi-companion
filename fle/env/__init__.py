"""Factorio environment module."""

# Suppress slpp SyntaxWarning about invalid escape sequences
from importlib import import_module
from typing import Any
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning, module="slpp")


def __getattr__(name: str) -> Any:
    if name in {"FactorioInstance", "DirectionInternal"}:
        module_name = "fle.env.instance"
    elif name in {"Prototype", "Resource"}:
        module_name = "fle.env.game_types"
    else:
        module_name = "fle.env.entities"
    try:
        value = getattr(import_module(module_name), name)
    except AttributeError as exc:
        raise AttributeError(name) from exc
    globals()[name] = value
    return value


__all__ = [
    "FactorioInstance",
    "DirectionInternal",
    "Direction",
    "Entity",
    "Position",
    "Inventory",
    "EntityGroup",
    "Prototype",
    "Resource",
]
