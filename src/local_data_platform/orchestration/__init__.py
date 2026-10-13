"""Orchestration: the parts of a lakehouse that make it run for the right time window.

0.1.3 builds this package up item by item. :mod:`local_data_platform.orchestration.windows` comes first: it holds
schedules, time zones, the window shortcuts, the placeholders of a source path and the filters that select the rows
of one window.
"""
