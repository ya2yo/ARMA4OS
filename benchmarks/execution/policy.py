"""Kernel scheduler preconditions for default-only experiments."""
from pathlib import Path

def verify_kernel_sched_ext_state() -> str:
    """Verify /sys/kernel/sched_ext/state for default-only execution.

    Returns 'kernel-default' ONLY if sched_ext is absent or explicitly 'disabled'.
    Refuses default-only execution if external sched_ext is enabled.
    """
    state_path = Path("/sys/kernel/sched_ext/state")
    if not state_path.exists():
        # absent = default sched class with unsupported sched_ext
        return "kernel-default"
    try:
        content = state_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Unable to read /sys/kernel/sched_ext/state: {exc}") from exc

    if content == "disabled":
        return "kernel-default"
    raise RuntimeError(
        f"Refusing default-only baseline: external sched_ext scheduler is active "
        f"(/sys/kernel/sched_ext/state reports '{content}'). "
        "Disable external sched_ext before executing default-only baseline."
    )
