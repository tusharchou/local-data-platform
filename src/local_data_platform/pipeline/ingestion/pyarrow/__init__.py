"""Legacy generic loader: :class:`PyArrowLoader` runs whatever pipeline a config names.

Before 0.1.1 ``PyArrowLoader`` expected ``source`` and ``target`` to be set by hand
and never set them. It now delegates to
:func:`local_data_platform.pipeline.registry.create_pipeline`, so it works for every
registered route. New code should call ``create_pipeline(config)`` directly.
"""

from typing import Any

import pyarrow as pa

from local_data_platform import Config
from local_data_platform.pipeline import Pipeline, PipelineResult
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.quality import QualityReport


class PyArrowLoader(Ingestion):
    """Load a config with the pipeline the registry picks for it.

    Args:
        config: The dataset config.
        **kwargs: Passed to :func:`~local_data_platform.pipeline.registry.create_pipeline`
            (``source``, ``target``, ``transforms``, ``checks``, ``on_failure``, ``name``, ``sink``).

    Attributes:
        pipeline: The delegate pipeline built by ``create_pipeline``. ``source``,
            ``target``, ``transforms``, ``checks``, ``on_failure``, ``name`` and
            ``sink`` are copied from it.

    Raises:
        PipelineNotFound: If no pipeline is registered for the config's route.
    """

    def __init__(self, config: Config, **kwargs: Any):
        from local_data_platform.pipeline.registry import create_pipeline

        self.pipeline: Pipeline = create_pipeline(config, **kwargs)
        delegate = self.pipeline
        super().__init__(config, source=delegate.source, target=delegate.target, transforms=delegate.transforms,
                         checks=delegate.checks, on_failure=delegate.on_failure, name=delegate.name,
                         sink=delegate.sink)

    def extract(self) -> pa.Table:
        """Read the source through the delegate pipeline."""
        return self.pipeline.extract()

    def _extract(self) -> pa.Table:
        """Pre-0.1.1 name of :meth:`extract`."""
        return self.extract()

    def transform(self, df: pa.Table) -> pa.Table:
        """Apply the delegate pipeline's transforms."""
        return self.pipeline.transform(df)

    def validate(self, df: pa.Table, context=None) -> QualityReport:
        """Run the delegate pipeline's quality checks."""
        return self.pipeline.validate(df, context)

    def run(self, mode: str | None = None, *, commit: Any = None, sink: Any = None) -> PipelineResult:
        """Run the delegate pipeline; see :meth:`Pipeline.run`."""
        return self.pipeline.run(mode, commit=commit, sink=sink)


__all__ = ["PyArrowLoader"]
