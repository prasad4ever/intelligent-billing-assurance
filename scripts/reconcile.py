"""Run draft-invoice reconciliation against the local SQLite database.

Usage (from the project root):

    python scripts/reconcile.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from billing_assurance.reconcile import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
