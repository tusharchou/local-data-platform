"""Pipeline registry and factory.

Pipelines register themselves for a route, a ``(source_format, target_format,
engine)`` key, with the :func:`register_pipeline` decorator. :func:`create_pipeline`
reads the route from a config and builds the registered class, so callers never
choose a pipeline class themselves::

    from local_data_platform import Config
    from local_data_platform.pipeline.registry import create_pipeline

    result = create_pipeline(Config.from_json("config/egression.json")).run()

The built-in pipelines (see :mod:`local_data_platform.pipeline.builtin`) are
registered on the first lookup, so nothing has to be imported first. The design
is explained in ``docs/design/factory_registry.md``.
"""

import importlib
from collections.abc import Callable
from enum import Enum
from typing import Any, NamedTuple, TypeVar

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError, PipelineNotFound
from local_data_platform.logger import get_logger
from local_data_platform.pipeline import Pipeline

logger = get_logger(__name__)

BUILTIN_MODULE = "local_data_platform.pipeline.builtin"

P = TypeVar("P", bound=type[Pipeline])


class Route(NamedTuple):
    """A registry key. Formats and engine are upper-case; ``engine`` is ``None`` when the route has none."""

    source_format: str
    target_format: str
    engine: str | None = None

    def __str__(self) -> str:
        text = f"{self.source_format} -> {self.target_format}"
        return f"{text} (engine {self.engine})" if self.engine else text


_REGISTRY: dict[Route, type[Pipeline]] = {}
_builtins_loaded = False


def _normalise(value: Any, what: str, optional: bool = False) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise ConfigError(f"pipeline {what} must be a non-empty string, got {value!r}")
    if not isinstance(value, str):
        raise ConfigError(f"pipeline {what} must be a string, got {type(value).__name__}")
    return value.strip().upper()


def make_route(source_format: Any, target_format: Any, engine: Any = None) -> Route:
    """Build a normalised :class:`Route`.

    Args:
        source_format: A format name such as ``"csv"`` or a ``SupportedFormat`` member.
        target_format: A format name or ``SupportedFormat`` member.
        engine: An engine name, a ``SupportedEngine`` member, or ``None``.

    Returns:
        The route, with upper-case names.

    Raises:
        ConfigError: If a format is missing or not a string.
    """
    return Route(
        _normalise(source_format, "source format"),
        _normalise(target_format, "target format"),
        _normalise(engine, "engine", optional=True),
    )


def register_pipeline(source_format: Any, target_format: Any, engine: Any = None, *,
                      replace: bool = False) -> Callable[[P], P]:
    """Class decorator that registers a pipeline for a route.

    Args:
        source_format: The config's ``source.format``, e.g. ``"CSV"``.
        target_format: The config's ``target.format``, e.g. ``"ICEBERG"``.
        engine: The config's ``source.engine``, when the route needs one (a JSON
            source with the ``BIGQUERY`` engine is a BigQuery query).
        replace: Allow replacing a different class already registered for the route.

    Returns:
        The decorator. It returns the class unchanged.

    Raises:
        TypeError: If the decorated object is not a :class:`Pipeline` subclass.
        ValueError: If another class is already registered for the route and
            ``replace`` is false.
    """
    route = make_route(source_format, target_format, engine)

    def decorator(cls: P) -> P:
        if not (isinstance(cls, type) and issubclass(cls, Pipeline)):
            raise TypeError(f"@register_pipeline expects a Pipeline subclass, got {cls!r}")
        existing = _REGISTRY.get(route)
        same = existing is not None and (existing is cls or (
            existing.__module__ == cls.__module__ and existing.__qualname__ == cls.__qualname__))
        if existing is not None and not same and not replace:
            raise ValueError(f"route {route} is already registered to {_describe(existing)}; "
                             "pass replace=True to replace it")
        _REGISTRY[route] = cls
        logger.debug("Registered pipeline %s for route %s", cls.__name__, route)
        return cls

    return decorator


def unregister_pipeline(source_format: Any, target_format: Any, engine: Any = None) -> type[Pipeline]:
    """Remove a route from the registry, for tests and plugins that replace built-ins.

    Returns:
        The class that was registered.

    Raises:
        PipelineNotFound: If nothing is registered for the route.
    """
    route = make_route(source_format, target_format, engine)
    try:
        return _REGISTRY.pop(route)
    except KeyError:
        raise PipelineNotFound(f"no pipeline is registered for {route}") from None


def _load_builtins() -> None:
    global _builtins_loaded
    if not _builtins_loaded:
        importlib.import_module(BUILTIN_MODULE)
        _builtins_loaded = True


def _describe(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def registered_pipelines() -> dict[Route, type[Pipeline]]:
    """Return the registered routes and their classes, sorted by route.

    The built-in pipelines are included. The dict is a copy: changing it doesn't
    change the registry.
    """
    _load_builtins()
    return dict(sorted(_REGISTRY.items(), key=lambda item: (*item[0][:2], item[0].engine or "")))


def get_pipeline_class(source_format: Any, target_format: Any, engine: Any = None) -> type[Pipeline]:
    """Return the pipeline class registered for a route, without building it.

    The engine must match exactly: a route registered with an engine is not used
    for a config without one, and the other way round.

    Args:
        source_format: The source format, e.g. ``"CSV"`` (case-insensitive).
        target_format: The target format, e.g. ``"ICEBERG"``.
        engine: The source engine, e.g. ``"BIGQUERY"``, or ``None``.

    Returns:
        The registered :class:`Pipeline` subclass.

    Raises:
        PipelineNotFound: If no pipeline is registered for the route. The message
            lists the registered routes.
    """
    route = make_route(source_format, target_format, engine)
    routes = registered_pipelines()
    if route in routes:
        return routes[route]
    available = "; ".join(f"{key} [{cls.__name__}]" for key, cls in routes.items()) or "none"
    raise PipelineNotFound(f"no pipeline is registered for {route}. Registered routes: {available}")


def create_pipeline(config: Config, **kwargs: Any) -> Pipeline:
    """Build the pipeline for a config.

    The route comes from ``config.source["format"]``, ``config.target["format"]``
    and the optional ``config.source["engine"]``.

    Args:
        config: The dataset config, e.g. from :meth:`Config.from_json`.
        **kwargs: Passed to the pipeline constructor, to override parts of the
            config: ``source``, ``target``, ``transforms``, ``checks``,
            ``on_failure`` or ``name``.

    Returns:
        A ready-to-run :class:`Pipeline`.

    Raises:
        TypeError: If ``config`` is not a :class:`Config`.
        PipelineNotFound: If no pipeline is registered for the config's route.
        ConfigError: If the config is incomplete for that pipeline.
    """
    if not isinstance(config, Config):
        raise TypeError(f"create_pipeline() expects a local_data_platform.Config, got {type(config).__name__}; "
                        "load one with Config.from_json(path)")
    config.validate()
    cls = get_pipeline_class(config.source["format"], config.target["format"], config.source.get("engine"))
    logger.info("Config %s uses pipeline %s", config.identifier, cls.__name__)
    return cls(config, **kwargs)


__all__ = [
    "Route",
    "create_pipeline",
    "get_pipeline_class",
    "make_route",
    "register_pipeline",
    "registered_pipelines",
    "unregister_pipeline",
]
