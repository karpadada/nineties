from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable


CompatibilityUpdater = Callable[[], bool]
_update_lock = threading.Lock()


def update_youtube_packages() -> bool:
    """Ask the installed launcher to refresh the mutable YouTube dependencies."""
    executable = os.environ.get("NINETIES_CONTROL_EXECUTABLE", "").strip()
    if not executable:
        return False

    with _update_lock:
        try:
            result = subprocess.run(
                [executable, "__update-youtube-packages"],
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return result.returncode == 0
