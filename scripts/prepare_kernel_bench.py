#!/usr/bin/env python3
"""Connect to the test VM and prepare the Linux kernel source and benchmark prerequisites."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Sequence

# Ensure project root is in sys.path when executed directly as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.deployment.transport import Connection, SSH_OPTIONS

DEFAULT_HOST = "root@192.168.66.128"
DEFAULT_DEST_ROOT = "/srv/bench"
DEFAULT_KERNEL_VERSION = "v6.14"
DEFAULT_KERNEL_REPO = "https://mirrors.ustc.edu.cn/linux.git"
FALLBACK_KERNEL_REPO = "https://mirrors.tuna.tsinghua.edu.cn/git/linux.git"
DEFAULT_LOCAL_CONFIG = "configs/benchmarks.formal.local.json"
DEFAULT_KCONFIG_TYPE = "tinyconfig"


def run_remote_ssh(
    connection: Connection,
    remote_command: str,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Execute a remote shell command via Connection SSH with timeout and clean output."""
    full_cmd = connection.ssh(remote_command)
    try:
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        raw_out = exc.stdout
        stdout_str = raw_out.decode("utf-8", errors="replace") if isinstance(raw_out, bytes) else (raw_out or "")
        return -1, stdout_str, f"Command timed out after {timeout} seconds"
    except Exception as exc:
        return -1, "", str(exc)


def run_remote_streaming(
    connection: Connection,
    remote_command: str,
    timeout: float = 1800.0,
) -> int:
    """Execute a long-running remote shell command and stream output directly to stdout/stderr."""
    full_cmd = connection.ssh(remote_command)
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except Exception as exc:
        print(f"\n[ERROR] Failed to spawn remote process: {exc}", file=sys.stderr)
        return -1

    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(f"\n[ERROR] Remote execution timed out after {timeout}s", file=sys.stderr)
        return -1
    except Exception as exc:
        print(f"\n[ERROR] Remote execution error: {exc}", file=sys.stderr)
        return -1


def build_remote_bash_command(script: str) -> str:
    """Return a transport-safe command that executes *script* on the guest.

    The Windows transport invokes ``ssh`` through PowerShell.  Passing a
    ``bash -c '...'`` command there requires nested shell quoting, and
    PowerShell's native-process argument conversion can remove the double
    quotes used by :func:`shlex.quote`.  The resulting command reaches the
    guest as ``bash -c '''...''''`` and fails as soon as the script contains a
    quoted string.  Encoding the script as base64 leaves only shell-safe
    characters in the SSH command while preserving the script byte-for-byte.
    """
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"printf %s {encoded} | base64 -d | bash"


def probe_guest_environment(connection: Connection) -> dict[str, str]:
    """Verify connectivity and retrieve CPU count and OS release from the guest VM."""
    code, out, err = run_remote_ssh(
        connection,
        "echo NPROC=$(nproc); uname -r; cat /etc/os-release 2>/dev/null || true",
        timeout=15.0,
    )
    if code != 0:
        raise RuntimeError(
            f"Cannot connect to guest VM via SSH ({connection.host}).\n"
            f"Please check:\n"
            f"  1. Is the guest VM powered on in VMware?\n"
            f"  2. Is the IP address correct ({connection.host})?\n"
            f"  3. Is SSH key authentication configured without password prompts?\n"
            f"Details: {err.strip() or out.strip()}"
        )

    nproc_match = re.search(r"NPROC=(\d+)", out)
    jobs = nproc_match.group(1) if nproc_match else "4"
    return {"jobs": jobs, "probe_output": out}


def build_guest_setup_script(
    dest_root: str,
    kernel_version: str,
    kernel_repo: str,
    fallback_repo: str,
    depth: int,
    install_deps: bool,
    build_schbench: bool,
    kconfig_type: str = DEFAULT_KCONFIG_TYPE,
    force_config: bool = False,
) -> str:
    """Generate a self-contained shell script to be executed on the guest."""
    ver_clean = kernel_version.lstrip("v")
    kernel_dir = f"{dest_root}/linux-{ver_clean}"
    kernel_config_path = f"{dest_root}/kernel-{kconfig_type}.config"

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"echo '=== [1/5] Preparing destination directories under {dest_root} ==='",
        f"mkdir -p {dest_root}",
        "",
    ]

    if install_deps:
        lines.extend([
            "echo '=== [2/5] Checking and installing build dependencies ==='",
            "if command -v dnf >/dev/null 2>&1; then",
            "    dnf install -y git gcc make flex bison bc elfutils-libelf-devel openssl-devel tar python3 || true",
            "elif command -v apt-get >/dev/null 2>&1; then",
            "    apt-get update -y && apt-get install -y git gcc make flex bison bc libelf-dev libssl-dev tar python3 || true",
            "fi",
            "",
        ])
    else:
        lines.extend([
            "echo '=== [2/5] Skipping dependency installation as requested ==='",
            "",
        ])

    lines.extend([
        f"echo '=== [3/5] Fetching Linux kernel source ({kernel_version}) ==='",
        f"if [ -d '{kernel_dir}' ] && [ -f '{kernel_dir}/Makefile' ]; then",
        f"    echo 'Kernel source directory already exists at {kernel_dir}.'",
        f"    echo 'Ensuring clean tree without in-tree build artifacts...'",
        f"    rm -f '{kernel_dir}/.config' '{kernel_dir}/vmlinux' '{kernel_dir}/include/config/auto.conf' '{kernel_dir}/include/generated/autoconf.h'",
        "else",
        f"    if [ -d '{kernel_dir}' ]; then",
        f"        echo 'Existing directory {kernel_dir} is incomplete or corrupted, cleaning up...'",
        f"        rm -rf '{kernel_dir}'",
        "    fi",
        f"    echo 'Cloning from {kernel_repo} (version/tag: {kernel_version}, depth: {depth})...'",
        f"    if ! git clone --depth {depth} --branch '{kernel_version}' '{kernel_repo}' '{kernel_dir}'; then",
        f"        echo 'Primary clone failed, attempting fallback repository: {fallback_repo}'",
        f"        if ! git clone --depth {depth} --branch '{kernel_version}' '{fallback_repo}' '{kernel_dir}'; then",
        f"            echo 'Secondary clone failed, attempting upstream kernel.org...'",
        f"            git clone --depth {depth} --branch '{kernel_version}' 'https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git' '{kernel_dir}'",
        "        fi",
        "    fi",
        f"    rm -f '{kernel_dir}/.config' '{kernel_dir}/vmlinux'",
        "fi",
        "",
        f"echo '=== [4/5] Preparing baseline kernel configuration (kernel-{kconfig_type}.config) ==='",
    ])
    if force_config:
        lines.append(f"rm -f '{kernel_config_path}'")
    lines.extend([
        f"if [ -f '{kernel_config_path}' ]; then",
        f"    echo 'Configuration already exists at {kernel_config_path} (use --force-config to regenerate)'",
        "else",
    ])

    if kconfig_type == "current":
        lines.extend([
            "    CURRENT_KCONFIG=\"/boot/config-$(uname -r)\"",
            "    if [ -f \"$CURRENT_KCONFIG\" ]; then",
            f"        echo \"Using running kernel config $CURRENT_KCONFIG as template...\"",
            f"        cp \"$CURRENT_KCONFIG\" '{kernel_config_path}'",
            "    else",
            "        echo 'No /boot/config found, falling back to defconfig...'",
            "        TEMP_O=$(mktemp -d /tmp/arma_kconfig_prep_XXXXXX)",
            f"        make -C '{kernel_dir}' O=\"$TEMP_O\" defconfig",
            f"        cp \"$TEMP_O/.config\" '{kernel_config_path}'",
            "        rm -rf \"$TEMP_O\"",
            "    fi",
        ])
    else:
        lines.extend([
            f"    echo 'Generating baseline configuration via make {kconfig_type}...'",
            "    TEMP_O=$(mktemp -d /tmp/arma_kconfig_prep_XXXXXX)",
            f"    make -C '{kernel_dir}' O=\"$TEMP_O\" {kconfig_type}",
            f"    cp \"$TEMP_O/.config\" '{kernel_config_path}'",
            "    rm -rf \"$TEMP_O\"",
        ])

    lines.extend([
        f"    rm -f '{kernel_dir}/.config'",
        "fi",
        "",
    ])

    if build_schbench:
        lines.extend([
            "echo '=== [5/5] Checking schbench tool ==='",
            "if [ -x '/usr/local/bin/schbench' ]; then",
            "    echo 'schbench already installed at /usr/local/bin/schbench'",
            "else",
            "    echo 'Building schbench from upstream...'",
            "    TEMP_SCH=$(mktemp -d /tmp/schbench_build_XXXXXX)",
            "    if git clone --depth 1 https://git.kernel.org/pub/scm/linux/kernel/git/mason/schbench.git \"$TEMP_SCH\"; then",
            "        make -C \"$TEMP_SCH\"",
            "        cp \"$TEMP_SCH/schbench\" /usr/local/bin/schbench",
            "        chmod +x /usr/local/bin/schbench",
            "        rm -rf \"$TEMP_SCH\"",
            "        echo 'schbench installed to /usr/local/bin/schbench'",
            "    else",
            "        echo '[WARN] Could not clone schbench; you may need to install it manually.'",
            "        rm -rf \"$TEMP_SCH\"",
            "    fi",
            "fi",
            "",
        ])
    else:
        lines.extend([
            "echo '=== [5/5] Skipping schbench build as requested ==='",
            "",
        ])

    lines.append("echo '=== VM Preparation Completed Successfully ==='")
    return "\n".join(lines)


def generate_local_config(
    output_path: Path,
    dest_root: str,
    kernel_version: str,
    jobs: int,
    kconfig_type: str = DEFAULT_KCONFIG_TYPE,
) -> None:
    """Generate a ready-to-use benchmarks.formal.local.json for the caller."""
    ver_clean = kernel_version.lstrip("v")
    cfg = {
        "schema_version": 1,
        "policies": ["default"],
        "timeout_sec": 7200,
        "kernel": {
            "source": f"{dest_root}/linux-{ver_clean}",
            "config": f"{dest_root}/kernel-{kconfig_type}.config",
            "jobs": jobs,
            "make": "/usr/bin/make",
        },
        "schbench": {
            "binary": "/usr/local/bin/schbench",
            "args": ["-m", "2", "-t", "4"],
            "runtime_sec": 30,
        },
        "mixed": {
            "min_overlap_fraction": 0.9,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"\n[OK] Local benchmark configuration created: {output_path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Connect to the VMware test guest and prepare Linux kernel source and tools.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"SSH host (default: {DEFAULT_HOST})")
    parser.add_argument(
        "--transport",
        choices=["auto", "windows", "native"],
        default="auto",
        help="SSH transport mechanism (auto, windows via powershell, native)",
    )
    parser.add_argument(
        "--dest-root",
        default=DEFAULT_DEST_ROOT,
        help=f"Target root directory on the guest (default: {DEFAULT_DEST_ROOT})",
    )
    parser.add_argument(
        "--kernel-version",
        default=DEFAULT_KERNEL_VERSION,
        help=f"Kernel git tag or branch to checkout (default: {DEFAULT_KERNEL_VERSION})",
    )
    parser.add_argument(
        "--kernel-repo",
        default=DEFAULT_KERNEL_REPO,
        help=f"Primary Git repository for Linux (default: {DEFAULT_KERNEL_REPO})",
    )
    parser.add_argument(
        "--fallback-repo",
        default=FALLBACK_KERNEL_REPO,
        help=f"Fallback Git repository for Linux (default: {FALLBACK_KERNEL_REPO})",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Git clone depth (default: 1 for shallow clone to save time/bandwidth)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Build parallelism (jobs). 0 to auto-detect from guest CPU count.",
    )
    parser.add_argument(
        "--kconfig-type",
        choices=["tinyconfig", "defconfig", "current"],
        default=DEFAULT_KCONFIG_TYPE,
        help="Kernel config baseline: tinyconfig (SchedCP paper, fast), defconfig (ASA paper, full), or current (/boot/config)",
    )
    parser.add_argument(
        "--force-config",
        action="store_true",
        help="Force regeneration of kernel config even if the config file already exists on the guest",
    )
    parser.add_argument(
        "--no-install-deps",
        action="store_true",
        help="Skip installing build dependencies (gcc, make, libelf, etc.) via package manager",
    )
    parser.add_argument(
        "--no-schbench",
        action="store_true",
        help="Skip building and installing schbench binary",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated script without executing it on the guest",
    )
    parser.add_argument(
        "--output-config",
        default=DEFAULT_LOCAL_CONFIG,
        help=f"Path to local JSON config to create (default: {DEFAULT_LOCAL_CONFIG})",
    )

    args = parser.parse_args(argv)

    print(f"Connecting to test guest: {args.host} (transport: {args.transport})...")
    connection = Connection(args.host, args.transport)

    try:
        env = probe_guest_environment(connection)
    except RuntimeError as exc:
        print(f"\n[ERROR] Connectivity check failed:\n{exc}", file=sys.stderr)
        return 1

    guest_jobs = int(env["jobs"])
    final_jobs = args.jobs if args.jobs > 0 else max(1, guest_jobs)
    print(f"Connection successful! Detected guest CPU cores: {guest_jobs}. Build jobs set to: {final_jobs}.")

    script_content = build_guest_setup_script(
        dest_root=args.dest_root,
        kernel_version=args.kernel_version,
        kernel_repo=args.kernel_repo,
        fallback_repo=args.fallback_repo,
        depth=args.depth,
        install_deps=not args.no_install_deps,
        build_schbench=not args.no_schbench,
        kconfig_type=args.kconfig_type,
        force_config=args.force_config,
    )

    remote_cmd = build_remote_bash_command(script_content)

    if args.dry_run:
        print("[DRY-RUN] Generated guest preparation script:")
        print("-" * 60)
        print(script_content)
        print("-" * 60)
        print(f"[DRY-RUN] Remote command: {remote_cmd[:100]}... (length: {len(remote_cmd)})")
        return 0

    print(f"Executing environment preparation on {args.host}...")
    exit_code = run_remote_streaming(connection, remote_cmd)
    if exit_code != 0:
        print(f"\n[ERROR] Guest preparation failed with exit code {exit_code}", file=sys.stderr)
        return exit_code

    # Generate local configuration file
    local_cfg_path = Path(args.output_config).resolve()
    generate_local_config(
        output_path=local_cfg_path,
        dest_root=args.dest_root,
        kernel_version=args.kernel_version,
        jobs=final_jobs,
        kconfig_type=args.kconfig_type,
    )

    print("\n" + "=" * 80)
    print("Setup completed! You can now execute formal benchmark experiments on the guest:")
    print(f"  CCACHE_DISABLE=1 python3 -m benchmarks formal \\")
    print(f"    --config {args.output_config} \\")
    print(f"    --output results/formal-run-001 --scenarios kernel_build schbench mixed \\")
    print(f"    --repeats 5 --warmup 1 --seed 42")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
