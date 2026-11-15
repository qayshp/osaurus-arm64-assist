"""Bounded GGUF v2/v3 metadata inspection; never read tensor payloads or DLLs."""
from pathlib import Path
import struct

MAX_HEADER_BYTES = 64 * 1024 * 1024
MAX_METADATA_ENTRIES = 100000
MAX_ARRAY_ITEMS = 2000000
MAX_STRING_BYTES = 1024 * 1024
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
               10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S",
               15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 32: "BF16"}


class _Reader:
    def __init__(self, stream, size):
        self.stream, self.limit = stream, min(size, MAX_HEADER_BYTES)

    def read(self, count):
        if count < 0 or self.stream.tell() + count > self.limit:
            raise ValueError("GGUF metadata exceeds its bounded header")
        data = self.stream.read(count)
        if len(data) != count:
            raise ValueError("Truncated GGUF metadata")
        return data

    def skip(self, count):
        if count < 0 or self.stream.tell() + count > self.limit:
            raise ValueError("GGUF metadata exceeds its bounded header")
        self.stream.seek(count, 1)

    def number(self, fmt):
        return struct.unpack("<" + fmt, self.read(struct.calcsize("<" + fmt)))[0]

    def string(self, capture=False):
        size = self.number("Q")
        if capture:
            if size > MAX_STRING_BYTES:
                raise ValueError("GGUF metadata string is too large")
            return self.read(size).decode("utf-8")
        self.skip(size)
        return None

    def value(self, kind, capture=False, depth=0):
        if kind in _SCALARS:
            value = self.number(_SCALARS[kind])
            return value if capture else None
        if kind == 8:
            return self.string(capture)
        if kind != 9 or depth > 4:
            raise ValueError("Unsupported GGUF metadata type")
        item_type, count = self.number("I"), self.number("Q")
        if count > MAX_ARRAY_ITEMS:
            raise ValueError("GGUF metadata array has too many items")
        if item_type in _SCALARS:
            self.skip(count * struct.calcsize("<" + _SCALARS[item_type]))
        else:
            for _ in range(count):
                self.value(item_type, False, depth + 1)
        return None


def inspect_gguf(path):
    """Return only small classification fields; tokenizer/template text is skipped."""
    path = Path(path)
    before = path.stat()
    captured = {}
    templates = False
    with path.open("rb") as stream:
        reader = _Reader(stream, before.st_size)
        if reader.read(4) != b"GGUF":
            raise ValueError("Not a supported little-endian GGUF file")
        version = reader.number("I")
        if version not in (2, 3):
            raise ValueError("Unsupported GGUF version")
        tensors, count = reader.number("Q"), reader.number("Q")
        if tensors > 10000000 or count > MAX_METADATA_ENTRIES:
            raise ValueError("GGUF header counts exceed bounds")
        for _ in range(count):
            key = reader.string(True)
            if len(key) > 1024:
                raise ValueError("GGUF metadata key is too long")
            kind = reader.number("I")
            is_template = key == "tokenizer.chat_template" or key.startswith("tokenizer.chat_template.")
            wanted = key in ("general.architecture", "general.type", "general.file_type", "split.count", "split.no", "split.tensors.count") or key.endswith(".context_length")
            if wanted and key in captured:
                raise ValueError("Duplicate GGUF classification key")
            if is_template:
                if kind != 8:
                    raise ValueError("GGUF chat template must be a string")
                size = reader.number("Q")
                templates = templates or size > 0
                reader.skip(size)
            else:
                value = reader.value(kind, wanted)
                if wanted:
                    if kind == 9:
                        raise ValueError("GGUF classification value cannot be an array")
                    captured[key] = value
        header_bytes = stream.tell()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("GGUF changed during metadata inspection")
    architecture = captured.get("general.architecture")
    general_type = captured.get("general.type")
    if architecture is not None and (not isinstance(architecture, str) or len(architecture) > 128):
        raise ValueError("Invalid GGUF architecture")
    if general_type is not None and (not isinstance(general_type, str) or len(general_type) > 64):
        raise ValueError("Invalid GGUF general type")
    split_count, split_no = captured.get("split.count", 1), captured.get("split.no", 0)
    if type(split_count) is not int or not 1 <= split_count <= 10000 or type(split_no) is not int or not 0 <= split_no < split_count:
        raise ValueError("Invalid GGUF split metadata")
    context = captured.get(str(architecture) + ".context_length")
    if context is not None and (type(context) is not int or not 1 <= context <= 2**31-1):
        raise ValueError("Invalid GGUF context length")
    file_type = captured.get("general.file_type")
    if file_type is not None and (type(file_type) is not int or not 0 <= file_type <= 1024):
        raise ValueError("Invalid GGUF file type")
    return {"version": version, "architecture": architecture, "general_type": general_type,
            "type": general_type, "has_chat_template": templates, "split_count": split_count,
            "split_no": split_no, "split_tensors_count": captured.get("split.tensors.count"),
            "file_type": file_type, "quantization": _FILE_TYPES.get(file_type),
            "context_length": context, "tensor_count": tensors, "size_bytes": before.st_size,
            "header_bytes": header_bytes}
