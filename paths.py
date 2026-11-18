"""
KIAROS file locations.

Everything KIAROS writes lives under DATA_DIR (default: ./data). Override with
KIAROS_DATA_DIR, and point at a different settings file with KIAROS_ENV_FILE —
the test suite uses both so it never touches your real memory or keys.
"""

import os
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = Path(os.getenv("KIAROS_DATA_DIR", str(ROOT / "data"))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = Path(os.getenv("KIAROS_ENV_FILE", str(ROOT / ".env"))).expanduser()
