"""Module entrypoint for `python -m ref_verify`."""

from __future__ import annotations

import sys

from ref_verify.cli import main

if __name__ == "__main__":
    sys.exit(main())
