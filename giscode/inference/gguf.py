import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


GGUF_MAGIC = b"GGUF"

GGUF_TYPE_UINT32 = 5
GGUF_TYPE_INT32 = 4
GGUF_TYPE_FLOAT32 = 6
GGUF_TYPE_BOOL = 7
GGUF_TYPE_STRING = 8
GGUF_TYPE_ARRAY = 9
GGUF_TYPE_UINT64 = 10
GGUF_TYPE_INT64 = 11
GGUF_TYPE_FLOAT64 = 12
GGUF_TYPE_UINT8 = 0
GGUF_TYPE_INT8 = 1
GGUF_TYPE_UINT16 = 2
GGUF_TYPE_INT16 = 3

_TYPE_BODY_SIZES = {
    GGUF_TYPE_UINT8: 1,
    GGUF_TYPE_INT8: 1,
    GGUF_TYPE_UINT16: 2,
    GGUF_TYPE_INT16: 2,
    GGUF_TYPE_UINT32: 4,
    GGUF_TYPE_INT32: 4,
    GGUF_TYPE_FLOAT32: 4,
    GGUF_TYPE_BOOL: 1,
    GGUF_TYPE_UINT64: 8,
    GGUF_TYPE_INT64: 8,
    GGUF_TYPE_FLOAT64: 8,
}


class GGUFReadError(Exception):
    pass


@dataclass
class GGUFMetadata:
    path: str
    file_size_bytes: int
    gguf_version: int
    architecture: Optional[str] = None
    name: Optional[str] = None
    context_length_train: Optional[int] = None
    block_count: Optional[int] = None
    attention_head_count: Optional[int] = None
    attention_head_count_kv: Optional[int] = None
    attention_key_length: Optional[int] = None
    attention_value_length: Optional[int] = None
    embedding_length: Optional[int] = None
    expert_count: Optional[int] = None
    expert_used_count: Optional[int] = None
    tensor_count: Optional[int] = None
    expert_tensor_bytes: Optional[int] = None
    other_tensor_bytes: Optional[int] = None
    layer_tensor_bytes: Optional[int] = None
    nonlayer_tensor_bytes: Optional[int] = None
    kv_block_count: Optional[int] = None
    output_tensor_bytes: Optional[int] = None
    nextn_tensor_bytes: Optional[int] = None
    nextn_predict_layers: Optional[int] = None
    ssm_state_size: Optional[int] = None
    ssm_inner_size: Optional[int] = None
    ssm_group_count: Optional[int] = None
    ssm_conv_kernel: Optional[int] = None
    ssm_time_step_rank: Optional[int] = None

    @property
    def head_dim(self) -> Optional[int]:
        if self.attention_key_length is not None and self.attention_key_length > 0:
            return self.attention_key_length
        if (
            self.embedding_length is not None
            and self.attention_head_count is not None
            and self.attention_head_count > 0
        ):
            return self.embedding_length // self.attention_head_count
        return None

    @property
    def is_moe(self) -> bool:
        if self.expert_count is not None and self.expert_count > 0:
            return True
        if (self.expert_tensor_bytes or 0) > 0:
            return True
        return False


def _resolve_key(raw_key: str) -> tuple[str, str]:
    """Return (qualified_key, suffix) where suffix is the part after the prefix dot.

    Examples:
        "qwen3vl.block_count" -> ("qwen3vl", "block_count")
        "general.architecture" -> ("general", "architecture")
        "block_count" -> ("", "block_count")
    """
    if "." in raw_key:
        prefix, _, suffix = raw_key.partition(".")
        return prefix, suffix
    return "", raw_key


def _read_scalar(data: bytes, cur: int, typ: int):
    """Parse a single scalar KV value. Returns (value, new_cur)."""
    if typ == GGUF_TYPE_STRING:
        if cur + 8 > len(data):
            raise GGUFReadError("truncated string length")
        sl = struct.unpack("<Q", data[cur : cur + 8])[0]
        cur += 8
        if sl > 262_144:
            raise GGUFReadError(f"string too long: {sl}")
        if cur + sl > len(data):
            raise GGUFReadError("truncated string body")
        value = data[cur : cur + sl].decode("utf-8")
        return value, cur + sl
    size = _TYPE_BODY_SIZES.get(typ)
    if size is None:
        raise GGUFReadError(f"unhandled scalar type: {typ}")
    if cur + size > len(data):
        raise GGUFReadError("truncated scalar body")
    if typ == GGUF_TYPE_UINT8:
        return data[cur], cur + 1
    if typ == GGUF_TYPE_INT8:
        return struct.unpack("<b", data[cur : cur + 1])[0], cur + 1
    if typ == GGUF_TYPE_UINT16:
        return struct.unpack("<H", data[cur : cur + 2])[0], cur + 2
    if typ == GGUF_TYPE_INT16:
        return struct.unpack("<h", data[cur : cur + 2])[0], cur + 2
    if typ == GGUF_TYPE_UINT32:
        return struct.unpack("<I", data[cur : cur + 4])[0], cur + 4
    if typ == GGUF_TYPE_INT32:
        return struct.unpack("<i", data[cur : cur + 4])[0], cur + 4
    if typ == GGUF_TYPE_FLOAT32:
        return struct.unpack("<f", data[cur : cur + 4])[0], cur + 4
    if typ == GGUF_TYPE_BOOL:
        return bool(data[cur]), cur + 1
    if typ == GGUF_TYPE_UINT64:
        return struct.unpack("<Q", data[cur : cur + 8])[0], cur + 8
    if typ == GGUF_TYPE_INT64:
        return struct.unpack("<q", data[cur : cur + 8])[0], cur + 8
    if typ == GGUF_TYPE_FLOAT64:
        return struct.unpack("<d", data[cur : cur + 8])[0], cur + 8
    raise GGUFReadError(f"scalar type not handled: {typ}")


def _skip_value(data: bytes, cur: int, typ: int) -> int:
    """Skip over a KV value body. Returns new offset."""
    if typ == GGUF_TYPE_ARRAY:
        # array: u32 element-type + u64 length + body
        if cur + 4 > len(data):
            raise GGUFReadError("truncated array header")
        el_type = struct.unpack("<I", data[cur : cur + 4])[0]
        cur += 4
        if cur + 8 > len(data):
            raise GGUFReadError("truncated array length")
        count = struct.unpack("<Q", data[cur : cur + 8])[0]
        cur += 8
        el_size = _TYPE_BODY_SIZES.get(el_type, 0)
        if el_type == GGUF_TYPE_STRING:
            for _ in range(count):
                if cur + 8 > len(data):
                    raise GGUFReadError("truncated array of strings")
                sl = struct.unpack("<Q", data[cur : cur + 8])[0]
                cur += 8 + sl
        else:
            cur += count * el_size
        return cur
    if typ == GGUF_TYPE_STRING:
        sl = struct.unpack("<Q", data[cur : cur + 8])[0]
        return cur + 8 + sl
    size = _TYPE_BODY_SIZES.get(typ)
    if size is None:
        raise GGUFReadError(f"cannot skip type: {typ}")
    return cur + size


def _scan_metadata(buffer: bytes) -> dict[str, object]:
    if len(buffer) < 24:
        raise GGUFReadError("buffer too small to be a GGUF header")
    if buffer[:4] != GGUF_MAGIC:
        raise GGUFReadError(f"not a GGUF file (magic = {buffer[:4]!r})")
    gguf_version = struct.unpack("<I", buffer[4:8])[0]
    tensor_count = struct.unpack("<Q", buffer[8:16])[0]
    kv_count = struct.unpack("<Q", buffer[16:24])[0]

    out: dict[str, object] = {}
    cur = 24
    complete = True
    for _ in range(kv_count):
        if cur + 8 > len(buffer):
            complete = False
            break
        key_len = struct.unpack("<Q", buffer[cur : cur + 8])[0]
        cur += 8
        if key_len > 512 or cur + key_len > len(buffer):
            complete = False
            break
        key = buffer[cur : cur + key_len].decode("utf-8", errors="replace")
        cur += key_len
        if cur + 4 > len(buffer):
            complete = False
            break
        typ = struct.unpack("<I", buffer[cur : cur + 4])[0]
        cur += 4
        try:
            if typ in (GGUF_TYPE_STRING, *(_TYPE_BODY_SIZES.keys())):
                value, cur = _read_scalar(buffer, cur, typ)
                out[key] = value
            elif typ == GGUF_TYPE_ARRAY:
                cur = _skip_value(buffer, cur, typ)
            else:
                cur = _skip_value(buffer, cur, typ)
        except GGUFReadError:
            complete = False
            break
    out["__gguf_version__"] = gguf_version
    out["__tensor_count__"] = int(tensor_count)
    if complete:
        out["__kv_end__"] = cur
    return out


def _is_expert_tensor(name: str) -> bool:
    return "_exps." in name or ".experts." in name or name.startswith("experts.")


def _parse_tensor_entries(
    buffer: bytes, kv_end: int, tensor_count: int, dims_u64: bool
) -> Optional[list[tuple[str, int]]]:
    offsets: list[tuple[str, int]] = []
    try:
        cur = kv_end
        dim_size = 8 if dims_u64 else 4
        dim_fmt = "<Q" if dims_u64 else "<I"
        for _ in range(tensor_count):
            name_len = struct.unpack("<Q", buffer[cur : cur + 8])[0]
            cur += 8
            if name_len == 0 or name_len > 512:
                return None
            name = buffer[cur : cur + name_len].decode("utf-8")
            cur += name_len
            n_dims = struct.unpack("<I", buffer[cur : cur + 4])[0]
            cur += 4
            if n_dims == 0 or n_dims > 8:
                return None
            for _ in range(n_dims):
                struct.unpack(dim_fmt, buffer[cur : cur + dim_size])
                cur += dim_size
            cur += 4  # ggml type
            offset = struct.unpack("<Q", buffer[cur : cur + 8])[0]
            cur += 8
            offsets.append((name, offset))
    except (struct.error, UnicodeDecodeError, ValueError):
        return None
    return offsets


def _read_tensor_table(
    buffer: bytes, kv_end: int, tensor_count: int, file_size: int, alignment: int
) -> Optional[dict[str, int]]:
    """Parse the tensor info table and derive per-group byte sizes.

    Tensor entries are (name, n_dims, dims, type, offset); data offsets are
    relative to the aligned data section start. Sizes are derived from
    consecutive offsets, so no GGML type-size table is needed. Both u32 and
    u64 dimension encodings are accepted; the first layout whose offsets are
    monotonic and inside the file wins.

    Returns None when the table is truncated or inconsistent.
    """
    offsets: Optional[list[tuple[str, int]]] = None
    dims_u64 = True
    for candidate_u64 in (True, False):
        parsed = _parse_tensor_entries(buffer, kv_end, tensor_count, candidate_u64)
        if not parsed:
            continue
        if all(parsed[i + 1][1] >= parsed[i][1] for i in range(len(parsed) - 1)):
            offsets = parsed
            dims_u64 = candidate_u64
            break
    if not offsets:
        return None
    cur = kv_end
    dim_size = 8 if dims_u64 else 4
    try:
        for _ in range(tensor_count):
            name_len = struct.unpack("<Q", buffer[cur : cur + 8])[0]
            cur += 8 + name_len
            n_dims = struct.unpack("<I", buffer[cur : cur + 4])[0]
            cur += 4 + dim_size * n_dims + 4 + 8
    except (struct.error, ValueError):
        return None
    table_end = cur
    if table_end > len(buffer):
        return None
    data_start = ((table_end + alignment - 1) // alignment) * alignment
    expert = other = layer = nonlayer = 0
    output_bytes = nextn = 0
    kv_blocks: set[int] = set()
    for i, (name, offset) in enumerate(offsets):
        if i + 1 < len(offsets):
            size = offsets[i + 1][1] - offset
        else:
            size = file_size - data_start - offset
        if size <= 0:
            return None
        if ".nextn." in name:
            nextn += size
            continue
        if _is_expert_tensor(name):
            expert += size
        else:
            other += size
        if name.startswith("blk."):
            layer += size
            if name.endswith("attn_k.weight"):
                try:
                    kv_blocks.add(int(name.split(".")[1]))
                except (IndexError, ValueError):
                    pass
        else:
            nonlayer += size
            if name == "output.weight":
                output_bytes += size
    if expert + other <= 0:
        return None
    if nextn > 0 and kv_blocks:
        # Speculative-decode draft block (contains .nextn. tensors) is ignored
        # by llama-server; exclude the highest block index from KV accounting.
        kv_blocks = {b for b in kv_blocks if b < max(kv_blocks)}
    return {
        "expert_bytes": expert,
        "other_bytes": other,
        "layer_bytes": layer,
        "nonlayer_bytes": nonlayer,
        "tensor_count": len(offsets),
        "kv_blocks": len(kv_blocks),
        "output_bytes": output_bytes,
        "nextn_bytes": nextn,
    }


def read_metadata(path: str | Path, max_kv_bytes: int = 48_000_000) -> GGUFMetadata:
    """Read the metadata block of a GGUF file. Only the metadata region is read;
    tensor data is ignored, so the call is bounded in both time and memory.

    When the buffer also covers the tensor info table, per-group tensor byte
    sizes (expert vs non-expert, layer vs non-layer) are derived from the
    tensor offset table.

    Raises:
        GGUFReadError: when the file cannot be parsed.
    """
    p = Path(path)
    if not p.exists():
        raise GGUFReadError(f"file not found: {p}")
    file_size = p.stat().st_size
    max_bytes = min(file_size, max_kv_bytes)
    with open(p, "rb") as handle:
        buffer = handle.read(max_bytes)

    raw = _scan_metadata(buffer)
    arch = raw.get("general.architecture") or raw.get("__arch__")
    arch = arch if isinstance(arch, str) else None
    gguf_version = int(raw.get("__gguf_version__", 0) or 0)

    def by_suffix(suffix: str) -> Optional[object]:
        if arch and isinstance(arch, str):
            v = raw.get(f"{arch}.{suffix}")
            if v is not None:
                return v
        # "llama." prefix is the historical default (covers LLaMA, Mistral, Qwen2, etc.)
        v = raw.get(f"llama.{suffix}")
        if v is not None:
            return v
        return None

    meta = GGUFMetadata(
        path=str(p),
        file_size_bytes=file_size,
        gguf_version=gguf_version,
        architecture=arch,
        name=raw.get("general.name") if isinstance(raw.get("general.name"), str) else None,
        context_length_train=_to_int(by_suffix("context_length")),
        block_count=_to_int(by_suffix("block_count")),
        attention_head_count=_to_int(by_suffix("attention.head_count")),
        attention_head_count_kv=_to_int(by_suffix("attention.head_count_kv")),
        attention_key_length=_to_int(by_suffix("attention.key_length")),
        attention_value_length=_to_int(by_suffix("attention.value_length")),
        embedding_length=_to_int(by_suffix("embedding_length")),
        expert_count=_to_int(by_suffix("expert_count")),
        expert_used_count=_to_int(by_suffix("expert_used_count")),
        nextn_predict_layers=_to_int(by_suffix("nextn_predict_layers")),
        ssm_state_size=_to_int(by_suffix("ssm.state_size")),
        ssm_inner_size=_to_int(by_suffix("ssm.inner_size")),
        ssm_group_count=_to_int(by_suffix("ssm.group_count")),
        ssm_conv_kernel=_to_int(by_suffix("ssm.conv_kernel")),
        ssm_time_step_rank=_to_int(by_suffix("ssm.time_step_rank")),
    )
    kv_end = raw.get("__kv_end__")
    tensor_count = _to_int(raw.get("__tensor_count__"))
    alignment = _to_int(raw.get("general.alignment")) or 32
    if kv_end is not None and tensor_count and tensor_count > 0:
        sizes = _read_tensor_table(buffer, int(kv_end), int(tensor_count), file_size, alignment)
        if sizes is not None:
            meta.tensor_count = sizes["tensor_count"]
            meta.expert_tensor_bytes = sizes["expert_bytes"]
            meta.other_tensor_bytes = sizes["other_bytes"]
            meta.layer_tensor_bytes = sizes["layer_bytes"]
            meta.nonlayer_tensor_bytes = sizes["nonlayer_bytes"]
            meta.kv_block_count = sizes["kv_blocks"]
            meta.output_tensor_bytes = sizes["output_bytes"]
            meta.nextn_tensor_bytes = sizes["nextn_bytes"]
    if meta.expert_tensor_bytes and meta.expert_count is None:
        meta.expert_count = 1
    return meta


def _to_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
