"""Kept for compatibility with the Phase 6 instructions: does the same as setup_app.

    python -m payops.db.setup_app        (use this)
"""

from __future__ import annotations

import sys

from payops.db.setup_app import main

if __name__ == "__main__":
    print("(setup_history is now setup_app; running that.)")
    sys.exit(main())
