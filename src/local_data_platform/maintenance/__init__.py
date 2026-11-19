"""Table maintenance: snapshot expiry that keeps the idempotency floor, and an orphan-file finder.

Contract: ``docs/design/v0_1_1_platform.md`` section C8.

* :func:`expire_snapshots` removes old snapshots from table metadata, but never the current snapshot, a
  branch or tag head, ``main``'s last ``retain_last`` snapshots, or a snapshot carrying
  ``ldp.idempotency-key`` newer than the floor (nor the ancestry linking it to its branch head).
* :func:`find_orphans` lists files under the table location that no metadata references. It deletes nothing.
* :func:`remove_orphans` does the same and, only with ``dry_run=False``, deletes them.
* :func:`delete_old_rows` drops the rows of a ``_ldp`` history table that came before a cutoff, and
  :func:`maintain_system_tables` does that for the four history tables of a catalog and then expires their snapshots and
  finds their orphan files.

The CLI is ``ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]``, and ``ldp maintain CONFIG --system
[--keep-days N]`` for the history tables, registered by :func:`local_data_platform.maintenance.cli.add_cli`.
"""

from local_data_platform.maintenance._common import IDEMPOTENCY_KEY, MaintenanceError
from local_data_platform.maintenance.cli import add_cli
from local_data_platform.maintenance.orphans import find_orphans, remove_orphans
from local_data_platform.maintenance.snapshots import expire_snapshots, expiry_supported, plan_expiry
from local_data_platform.maintenance.system import delete_old_rows, maintain_system_tables

__all__ = ["IDEMPOTENCY_KEY", "MaintenanceError", "add_cli", "delete_old_rows", "expire_snapshots", "expiry_supported",
           "find_orphans", "maintain_system_tables", "plan_expiry", "remove_orphans"]
