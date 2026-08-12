"""LLM utilities for agents package.

The public exports are loaded lazily so lightweight consumers can reuse the
upstream policy parser without importing every optional provider dependency.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "APIFactory": ("fle.agents.llm.api_factory", "APIFactory"),
    "Policy": ("fle.agents.llm.parsing", "Policy"),
    "PolicyMeta": ("fle.agents.llm.parsing", "PolicyMeta"),
    "PythonParser": ("fle.agents.llm.parsing", "PythonParser"),
    "TimingTracker": ("fle.agents.llm.metrics", "TimingTracker"),
    "timing_tracker": ("fle.agents.llm.metrics", "timing_tracker"),
    "track_timing": ("fle.agents.llm.metrics", "track_timing"),
    "track_timing_async": ("fle.agents.llm.metrics", "track_timing_async"),
    "log_metrics": ("fle.agents.llm.metrics", "log_metrics"),
    "print_metrics": ("fle.agents.llm.metrics", "print_metrics"),
    "format_messages_for_anthropic": (
        "fle.agents.llm.utils",
        "format_messages_for_anthropic",
    ),
    "format_messages_for_openai": (
        "fle.agents.llm.utils",
        "format_messages_for_openai",
    ),
    "has_image_content": ("fle.agents.llm.utils", "has_image_content"),
    "merge_contiguous_messages": (
        "fle.agents.llm.utils",
        "merge_contiguous_messages",
    ),
    "remove_whitespace_blocks": (
        "fle.agents.llm.utils",
        "remove_whitespace_blocks",
    ),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    # API
    "APIFactory",
    # Parsing
    "Policy",
    "PolicyMeta",
    "PythonParser",
    # Metrics
    "TimingTracker",
    "timing_tracker",
    "track_timing",
    "track_timing_async",
    "log_metrics",
    "print_metrics",
    # Utils
    "format_messages_for_anthropic",
    "format_messages_for_openai",
    "has_image_content",
    "merge_contiguous_messages",
    "remove_whitespace_blocks",
]
