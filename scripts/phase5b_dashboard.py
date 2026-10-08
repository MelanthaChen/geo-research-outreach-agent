"""Convenience launcher for the local Phase 5B professor dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from outreach_agent.dashboard import main  # noqa: E402


if __name__ == "__main__":
    main()
