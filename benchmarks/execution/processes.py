"""POSIX process-group cleanup for bounded workload execution."""
from __future__ import annotations

import os
import signal

def kill_process_group(pgid: int) -> None:
    """Kill an entire POSIX process group, including any spawned descendants."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass

def is_process_group_alive(pgid: int) -> bool:
    """Check if any processes in the given process group are still alive."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False
