"""Table maintenance: snapshot expiry that keeps the idempotency floor, and an orphan-file finder.

Contract: ``docs/design/v0_2_0.md`` section C8.

* :func:`expire_snapshots` removes old snapshots from table metadata, but never the current snapshot, a
  branch or tag head, ``main``'s last ``retain_last`` snapshots, or a snapshot carrying
  ``ldp.idempotency-key`` newer than the floor (nor the ancestry linking it to its branch head).
* :func:`find_orphans` lists files under the table location that no metadata references. It deletes nothing.
* :func:`remove_orphans` does the same and, only with ``dry_run=False``, deletes them.

The CLI is ``ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]``, registered by
:func:`local_data_platform.maintenance.cli.add_cli`.
"""

from local_data_platform.maintenance._common import IDEMPOTENCY_KEY, MaintenanceError
from local_data_platform.maintenance.cli import add_cli
from local_data_platform.maintenance.orphans import find_orphans, remove_orphans
from local_data_platform.maintenance.snapshots import expire_snapshots, expiry_supported, plan_expiry

__all__ = ["IDEMPOTENCY_KEY", "MaintenanceError", "add_cli", "expire_snapshots", "expiry_supported",
           "find_orphans", "plan_expiry", "remove_orphans"]
