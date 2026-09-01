"""Load the standalone SDK source tree without installing it into the core project."""

from __future__ import annotations

import sys
from pathlib import Path


SDK_SOURCE = Path(__file__).parents[2] / "packaging" / "sdk" / "src"
sys.path.insert(0, str(SDK_SOURCE))
