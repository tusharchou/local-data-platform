"""``python -m local_data_platform.mcp_server``: the ``ldp mcp`` command on its own."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
