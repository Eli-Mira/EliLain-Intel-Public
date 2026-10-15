"""Pytest configuration for news tracker."""

import sys
from pathlib import Path

# Add tracker-core to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "tracker-core"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
