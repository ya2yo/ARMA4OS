"""Deterministic compression workload for ARMA4OS benchmarks.

Provides a repeatable, CPU-bound batch compression and decompression workload
using standard library algorithms (zlib, gzip, bz2, lzma).
Outputs structured JSON performance metrics to stdout.
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import lzma
import random
import sys
import time
import zlib
from typing import Callable, Tuple


def generate_deterministic_payload(size_bytes: int, seed: int) -> bytes:
    """Generate a reproducible pseudo-structured byte payload with realistic compressibility."""
    rng = random.Random(seed)
    # A dictionary of words to create compressible text/log-like structures
    words = [
        b"kernel", b"sched_ext", b"process", b"latency", b"throughput",
        b"bpfland", b"rusty", b"default", b"cgroup", b"cpu_burst",
        b"dispatch", b"queue", b"wakeup", b"migration", b"topology",
        b"context_switch", b"task_struct", b"runqueue", b"affinity", b"workload",
    ]
    chunks: list[bytes] = []
    generated = 0

    while generated < size_bytes:
        pattern_type = rng.randint(0, 3)
        if pattern_type == 0:
            # Word sequence
            word = rng.choice(words) + b" "
            chunks.append(word)
            generated += len(word)
        elif pattern_type == 1:
            # Structured hex / timestamp line
            timestamp = rng.randint(1700000000, 1800000000)
            line = f"[{timestamp}] pid={rng.randint(1, 65535)} event={rng.randint(0, 255)}\n".encode("ascii")
            chunks.append(line)
            generated += len(line)
        elif pattern_type == 2:
            # Repetitive byte block
            byte_val = bytes([rng.randint(0, 255)])
            repeat_len = rng.randint(8, 64)
            block = byte_val * repeat_len
            chunks.append(block)
            generated += len(block)
        else:
            # Random high-entropy slice
            rand_len = rng.randint(16, 64)
            block = rng.randbytes(rand_len)
            chunks.append(block)
            generated += len(block)

    full_payload = b"".join(chunks)
    return full_payload[:size_bytes]


def get_compressor_decompressor(algorithm: str) -> Tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]:
    """Return compression and decompression functions for the requested algorithm."""
    if algorithm == "zlib":
        return zlib.compress, zlib.decompress
    elif algorithm == "gzip":
        return gzip.compress, gzip.decompress
    elif algorithm == "bz2":
        return bz2.compress, bz2.decompress
    elif algorithm == "lzma":
        return lzma.compress, lzma.decompress
    else:
        raise ValueError(f"Unsupported compression algorithm: {algorithm}")


def run_compression_workload(
    size_bytes: int,
    iterations: int,
    seed: int,
    algorithm: str,
) -> dict[str, object]:
    """Execute the deterministic compression workload and return performance metrics."""
    compress_fn, decompress_fn = get_compressor_decompressor(algorithm)
    payload = generate_deterministic_payload(size_bytes, seed)
    orig_sha256 = hashlib.sha256(payload).hexdigest()

    last_compressed_len = 0
    t0 = time.perf_counter()

    for _ in range(iterations):
        compressed = compress_fn(payload)
        last_compressed_len = len(compressed)
        decompressed = decompress_fn(compressed)
        dec_sha256 = hashlib.sha256(decompressed).hexdigest()
        if dec_sha256 != orig_sha256:
            raise ValueError("Data corruption detected: decompressed SHA-256 does not match original")

    t1 = time.perf_counter()
    elapsed_s = max(t1 - t0, 1e-9)
    total_bytes_processed = size_bytes * iterations
    throughput_mb_s = (total_bytes_processed / (1024 * 1024)) / elapsed_s
    ratio = last_compressed_len / size_bytes if size_bytes > 0 else 0.0

    return {
        "workload": "compression",
        "algorithm": algorithm,
        "input_bytes": size_bytes,
        "compressed_bytes": last_compressed_len,
        "ratio": round(ratio, 4),
        "iterations": iterations,
        "elapsed_s": round(elapsed_s, 6),
        "throughput_mb_s": round(throughput_mb_s, 3),
        "sha256": orig_sha256,
        "verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic compression benchmark workload")
    parser.add_argument("--size-kb", type=int, default=1024, help="Input payload size in KB (default: 1024)")
    parser.add_argument("--iterations", type=int, default=3, help="Number of compress/decompress iterations (default: 3)")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic random seed (default: 42)")
    parser.add_argument(
        "--algorithm",
        type=str,
        default="zlib",
        choices=["zlib", "gzip", "bz2", "lzma"],
        help="Compression algorithm (default: zlib)",
    )

    args = parser.parse_args(argv)

    try:
        size_bytes = args.size_kb * 1024
        metrics = run_compression_workload(
            size_bytes=size_bytes,
            iterations=args.iterations,
            seed=args.seed,
            algorithm=args.algorithm,
        )
        print(json.dumps(metrics, ensure_ascii=False))
        return 0
    except Exception as exc:
        err_msg = {"workload": "compression", "status": "error", "error": str(exc)}
        print(json.dumps(err_msg, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
