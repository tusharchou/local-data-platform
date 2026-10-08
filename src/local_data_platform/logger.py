"""Logging helpers.

A library must not configure logging for the application that imports it, so this
module only hands out named loggers under the ``local_data_platform`` namespace.
Applications (and the ``ldp`` CLI) decide handlers and levels themselves.
"""

import logging
import warnings

PACKAGE_LOGGER = "local_data_platform"

logging.getLogger(PACKAGE_LOGGER).addHandler(logging.NullHandler())


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger in the package namespace, e.g. ``local_data_platform.format.csv``."""
    if not name or name == PACKAGE_LOGGER:
        return logging.getLogger(PACKAGE_LOGGER)
    if name.startswith(PACKAGE_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{PACKAGE_LOGGER}.{name}")


def log() -> logging.Logger:
    """Deprecated alias for :func:`get_logger`, kept for pre-0.1.1 imports. It will be removed in 0.2.0."""
    warnings.warn("local_data_platform.logger.log() is deprecated and will be removed in 0.2.0. Use get_logger()",
                  DeprecationWarning, stacklevel=2)
    return get_logger()


def configure_cli_logging(level: int = logging.INFO) -> None:
    """Configure a simple console handler. Used by the ``ldp`` CLI and the demo only."""
    logger = logging.getLogger(PACKAGE_LOGGER)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
               for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
