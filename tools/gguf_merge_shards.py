#!/usr/bin/env python3
"""Merge a llama.cpp multi-shard GGUF model (split.count/split.no metadata)
into a single-file GGUF, using gguf.GGUFReader/GGUFWriter primitives (no
official llama-gguf-split binary available on this host).

Usage: gguf_merge_shards.py <shard-00001-of-000NN.gguf> <output.gguf>
The remaining shards are auto-discovered by replacing the "-of-NNNNN.gguf"
suffix index.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import gguf

SPLIT_KEYS = {"split.no", "split.count", "split.tensors.count"}


def discover_shards(first_shard: Path) -> list[Path]:
    m = re.match(r"^(.*-)(\d+)(-of-)(\d+)(\.gguf)$", first_shard.name)
    if not m:
        raise ValueError(f"Not a recognized split-GGUF filename: {first_shard.name}")
    prefix, _, mid, total_str, ext = m.groups()
    total = int(total_str)
    width = len(total_str)
    shards = [first_shard.parent / f"{prefix}{i:0{width}d}{mid}{total_str}{ext}" for i in range(1, total + 1)]
    for s in shards:
        if not s.exists():
            raise FileNotFoundError(f"Missing shard: {s}")
    return shards


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    first_shard = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    shards = discover_shards(first_shard)
    print(f"[merge] {len(shards)} shards discovered:")
    for s in shards:
        print(f"  - {s.name}")

    readers = [gguf.GGUFReader(str(s), "r") for s in shards]
    base = readers[0]

    arch_field = base.get_field(gguf.Keys.General.ARCHITECTURE)
    arch = arch_field.contents() if arch_field else None
    if not arch:
        raise RuntimeError("Could not determine general.architecture from shard 0")
    print(f"[merge] architecture: {arch}")

    expected_tensors = base.get_field("split.tensors.count")
    expected_tensors = expected_tensors.contents() if expected_tensors else None
    actual_tensors = sum(len(r.tensors) for r in readers)
    print(f"[merge] tensors: expected={expected_tensors} actual_total={actual_tensors}")
    if expected_tensors is not None and actual_tensors != expected_tensors:
        raise RuntimeError(f"Tensor count mismatch: expected {expected_tensors}, got {actual_tensors}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = gguf.GGUFWriter(str(output_path), arch=arch, endianess=base.endianess)

    alignment_field = base.get_field(gguf.Keys.General.ALIGNMENT)
    if alignment_field is not None:
        writer.data_alignment = alignment_field.contents()

    # --- metadata: copy shard 0's KV, skip virtual/arch/split-bookkeeping keys ---
    n_kv = 0
    for field in base.fields.values():
        name = field.name
        if name == gguf.Keys.General.ARCHITECTURE or name.startswith("GGUF."):
            continue
        if name in SPLIT_KEYS:
            continue
        val_type = field.types[0]
        sub_type = field.types[-1] if val_type == gguf.GGUFValueType.ARRAY else None
        value = field.contents()
        if value is None:
            continue
        writer.add_key_value(name, value, val_type, sub_type=sub_type)
        n_kv += 1
    print(f"[merge] copied {n_kv} metadata keys")

    # --- tensor info + data, concatenated across shards in shard order ---
    total_bytes = 0
    for r in readers:
        for tensor in r.tensors:
            writer.add_tensor_info(tensor.name, tensor.data.shape, tensor.data.dtype, tensor.data.nbytes, tensor.tensor_type)
            total_bytes += tensor.n_bytes

    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_ti_data_to_file()

    written = 0
    for r in readers:
        for tensor in r.tensors:
            writer.write_tensor_data(tensor.data, tensor_endianess=r.endianess)
            written += tensor.n_bytes
            if written % (1 << 30) < tensor.n_bytes:
                print(f"[merge] ... {written / (1 << 30):.1f} / {total_bytes / (1 << 30):.1f} GiB")

    writer.close()
    print(f"[merge] done: {output_path} ({total_bytes / (1 << 30):.2f} GiB of tensor data)")


if __name__ == "__main__":
    main()
