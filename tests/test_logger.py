"""The package logger helpers in ``local_data_platform.logger``."""

import logging

import pytest

from local_data_platform.logger import PACKAGE_LOGGER, get_logger, log


def test_get_logger_keeps_every_logger_in_the_package_namespace():
    assert get_logger().name == PACKAGE_LOGGER
    assert get_logger("format.csv").name == f"{PACKAGE_LOGGER}.format.csv"
    assert get_logger(f"{PACKAGE_LOGGER}.events").name == f"{PACKAGE_LOGGER}.events"


def test_log_is_a_deprecated_alias_of_get_logger():
    with pytest.warns(DeprecationWarning, match="removed in 0.2.0"):
        logger = log()
    assert logger is logging.getLogger(PACKAGE_LOGGER)
