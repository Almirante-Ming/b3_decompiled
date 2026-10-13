#!/usr/bin/env python3
"""
Standalone IDA FLIRT (.sig) matcher — no IDA, no Ghidra/Jython needed.

Ported from the FLIRT-parsing core of NWMonster/ApplySig
(https://github.com/NWMonster/ApplySig), which itself derives from
thebabush/nampa and radare2's libr/anal/flirt.c. That script only runs
inside Ghidra's (now-removed) Jython console; this file keeps just the
pure parsing/matching logic and adds its own driver: given a binary and a
list of candidate function start addresses (e.g. exported from an already
analyzed Ghidra project via ExportFunctionAddresses.java), it tries every
.sig file against every candidate address and reports matches in the same
TSV shape tools/signatures/parse_matches.py already produces, so they can
be applied with tools/ghidra/scripts/ApplyLibraryMatches.java.

Usage:
    flirt_match.py <xbe_or_elf> <func_addrs.tsv> <sig_dir_or_file> \
        --out matches.tsv [--xbe]

If --xbe is passed, <xbe_or_elf> is read as a native .xbe (section table
gives VA -> file-offset mapping). Otherwise it's treated as a flat image
already mapped 1:1 at its own base (e.g. our synthetic ELF's section
payloads aren't contiguous with VA, so prefer --xbe against the original
dump's default.xbe).
"""
import argparse
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "xbe"))
from xbe_to_elf import parse_xbe  # noqa: E402

FLIRT_NAME_MAX = 1024


# ---------------- binary readers ----------------

def read_x(fmt, f, length):
    return struct.unpack(fmt, f.read(length))[0]


def read_u8(f):
    return read_x("B", f, 1)


def read_u16be(f):
    return read_x(">H", f, 2)


def read_u24be(f):
    return read_u8(f) << 16 | read_u16be(f)


def read_u32be(f):
    return read_x(">L", f, 4)


def read_u16le(f):
    return read_x("<H", f, 2)


def read_u32le(f):
    return read_x("<L", f, 4)


# ---------------- CRC16 (X.25) ----------------

POLY = 0x1021
_crc_table = []


def _rev8(n):
    return int("{:08b}".format(n)[::-1], 2)


def _rev16(n):
    return int("{:016b}".format(n)[::-1], 2)


def _init_table():
    for i in range(256):
        i = _rev8(i)
        crc = 0
        c = (i << 8) & 0xFFFF
        for _ in range(8):
            if (crc ^ c) & 0x8000:
                crc = (crc << 1) ^ POLY
            else:
                crc = crc << 1
            crc &= 0xFFFF
            c = (c << 1) & 0xFFFF
        crc = _rev16(crc)
        _crc_table.append(crc)


_init_table()


def crc16(data, start_value=0xFFFF):
    out = start_value
    for b in data:
        tmp = (out ^ b) & 0xFF
        out = (out >> 8) ^ _crc_table[tmp]
    out ^= 0xFFFF
    out = ((out & 0xFF) << 8) | ((out >> 8) & 0xFF)
    return out


# ---------------- FLIRT structures ----------------

class FlirtFunctionFlag:
    FUNCTION_LOCAL = 0x02
    FUNCTION_UNRESOLVED_COLLISION = 0x08


class FlirtParseFlag:
    PARSE_MORE_PUBLIC_NAMES = 0x01
    PARSE_READ_TAIL_BYTES = 0x02
    PARSE_READ_REFERENCED_FUNCTIONS = 0x04
    PARSE_MORE_MODULES_WITH_SAME_CRC = 0x08
    PARSE_MORE_MODULES = 0x10


class FlirtFeatureFlag:
    FEATURE_COMPRESSED = 0x10


class FlirtException(Exception):
    pass


class FlirtFunction:
    def __init__(self, name, offset, is_local, is_collision):
        self.name = name
        self.offset = offset
        self.is_local = is_local
        self.is_collision = is_collision


class FlirtModule:
    def __init__(self, crc_length, crc16_value, public_functions, tail_bytes):
        self.crc_length = crc_length
        self.crc16 = crc16_value
        self.public_functions = public_functions
        self.tail_bytes = tail_bytes


class FlirtNode:
    def __init__(self, children, modules, length, variant_mask, pattern):
        self.children = children
        self.modules = modules
        self.length = length
        self.variant_mask = variant_mask
        self.pattern = pattern


class FlirtHeader:
    def __init__(self, version, n_functions, library_name):
        self.version = version
        self.n_functions = n_functions
        self.library_name = library_name


class FlirtFile:
    def __init__(self, header, root):
        self.header = header
        self.root = root


def read_max_2_bytes(f):
    b = read_u8(f)
    if b & 0x80 == 0x80:
        return ((b & 0x7F) << 8) | read_u8(f)
    return b


def read_multiple_bytes(f):
    b = read_u8(f)
    if b & 0x80 != 0x80:
        return b
    elif b & 0xC0 != 0xC0:
        return ((b & 0x7F) << 8) | read_u8(f)
    elif b & 0xE0 != 0xE0:
        return ((b & 0x3F) << 24) | read_u24be(f)
    else:
        return read_u32be(f)


def read_node_variant_mask(f, length):
    if length < 0x10:
        return read_max_2_bytes(f)
    elif length <= 0x20:
        return read_multiple_bytes(f)
    elif length <= 0x40:
        return (read_multiple_bytes(f) << 32) | read_multiple_bytes(f)
    raise FlirtException(f"Wrong node variant mask length: {length}")


def read_node_bytes(f, length, variant_mask):
    mask_bit = 1 << (length - 1)
    variant_bools = []
    pattern = []
    for _ in range(length):
        curr = variant_mask & mask_bit != 0
        pattern.append(0 if curr else read_u8(f))
        variant_bools.append(curr)
        mask_bit >>= 1
    return variant_bools, pattern


def parse_header(f):
    magic = f.read(6)
    if magic != b"IDASGN":
        raise FlirtException("Wrong file type")
    version = read_u8(f)
    if version < 5 or version > 10:
        raise FlirtException(f"Unknown version: {version}")
    read_u8(f)  # arch
    read_u32le(f)  # file_types
    read_u16le(f)  # os_types
    read_u16le(f)  # app_types
    features = read_u16le(f)
    read_u16le(f)  # old_n_functions
    read_u16le(f)  # crc16
    f.read(12)  # ctype
    library_name_len = read_u8(f)
    read_u16le(f)  # ctypes_crc16

    n_functions = None
    if version >= 6:
        n_functions = read_u32le(f)
        if version >= 8:
            read_u16le(f)  # pattern_size
            if version > 9:
                read_u16le(f)  # unknown

    library_name = f.read(library_name_len).decode("latin1")
    return FlirtHeader(version, n_functions, library_name), features


def parse_tail_byte(f, version):
    offset = read_multiple_bytes(f) if version >= 9 else read_max_2_bytes(f)
    value = read_u8(f)
    return offset, value


def parse_tail_bytes(f, version):
    length = read_u8(f) if version >= 8 else 1
    return [parse_tail_byte(f, version) for _ in range(length)]


def parse_referenced_function(f, version):
    offset = read_multiple_bytes(f) if version >= 9 else read_max_2_bytes(f)
    name_length = read_u8(f)
    if name_length == 0:
        name_length = read_multiple_bytes(f)
    name = f.read(name_length)
    if name and name[-1] == 0:
        name = name[:-1]
    return offset, name


def parse_referenced_functions(f, version):
    length = read_u8(f) if version >= 8 else 1
    return [parse_referenced_function(f, version) for _ in range(length)]


def parse_public_function(f, version, offset):
    is_local = False
    is_collision = False
    offset += read_multiple_bytes(f) if version >= 9 else read_max_2_bytes(f)

    b = read_u8(f)
    if b < 0x20:
        if b & FlirtFunctionFlag.FUNCTION_LOCAL:
            is_local = True
        if b & FlirtFunctionFlag.FUNCTION_UNRESOLVED_COLLISION:
            is_collision = True
        b = read_u8(f)

    name = bytearray()
    finished = False
    for _ in range(FLIRT_NAME_MAX):
        if b < 0x20:
            finished = True
            break
        name.append(b)
        b = read_u8(f)
    flags = b
    if not finished:
        pass  # name too long; keep what we have
    return FlirtFunction(name.decode("latin1"), offset, is_local, is_collision), offset, flags


def parse_module(f, version, crc_length, crc16_value):
    length = read_multiple_bytes(f) if version >= 9 else read_max_2_bytes(f)
    _ = length
    public_functions = []
    offset = 0
    flags = 0
    while True:
        func, offset, flags = parse_public_function(f, version, offset)
        public_functions.append(func)
        if flags & FlirtParseFlag.PARSE_MORE_PUBLIC_NAMES == 0:
            break

    tail_bytes = []
    if flags & FlirtParseFlag.PARSE_READ_TAIL_BYTES != 0:
        tail_bytes = parse_tail_bytes(f, version)
    if flags & FlirtParseFlag.PARSE_READ_REFERENCED_FUNCTIONS != 0:
        parse_referenced_functions(f, version)

    return FlirtModule(crc_length, crc16_value, public_functions, tail_bytes), flags


def parse_modules(f, version):
    modules = []
    while True:
        crc_length = read_u8(f)
        crc16_value = read_u16be(f)
        while True:
            module, flags = parse_module(f, version, crc_length, crc16_value)
            modules.append(module)
            if flags & FlirtParseFlag.PARSE_MORE_MODULES_WITH_SAME_CRC == 0:
                break
        if flags & FlirtParseFlag.PARSE_MORE_MODULES == 0:
            break
    return modules


def parse_tree(f, version, is_root):
    if is_root:
        length, variant_mask, pattern = 0, None, None
    else:
        length = read_u8(f)
        variant_mask = read_node_variant_mask(f, length)
        variant_mask, pattern = read_node_bytes(f, length, variant_mask)

    nodes = read_multiple_bytes(f)
    if nodes == 0:
        modules = parse_modules(f, version)
        return FlirtNode([], modules, length, variant_mask, pattern)

    children = [parse_tree(f, version, False) for _ in range(nodes)]
    return FlirtNode(children, [], length, variant_mask, pattern)


def parse_flirt_file(path):
    with open(path, "rb") as f:
        header, features = parse_header(f)
        data = f.read()
    if features & FlirtFeatureFlag.FEATURE_COMPRESSED:
        if header.version == 5:
            raise FlirtException("Compression unsupported on flirt v5")
        data = zlib.decompress(data)
    import io
    f2 = io.BytesIO(data)
    root = parse_tree(f2, header.version, is_root=True)
    return FlirtFile(header, root)


# ---------------- matching ----------------

def match_node_pattern(node, buf, offset):
    if len(buf) < offset + len(node.pattern):
        return False
    for i in range(len(node.pattern)):
        if node.variant_mask[i]:
            continue
        if buf[offset + i] != node.pattern[i]:
            return False
    return True


def match_module(module, buf, offset, found):
    buf_size = len(buf) - offset
    if module.crc_length <= buf_size:
        if crc16(buf[offset:offset + module.crc_length]) != module.crc16:
            return False
    for tb_offset, tb_value in module.tail_bytes:
        pos = offset + module.crc_length + tb_offset
        if pos < len(buf) and buf[pos] != tb_value:
            return False
    for func in module.public_functions:
        found.append(func)
    return True


def match_node(node, buf, offset, found):
    if not match_node_pattern(node, buf, offset):
        return False
    for child in node.children:
        if match_node(child, buf, offset + node.length, found):
            return True
    for module in node.modules:
        if match_module(module, buf, offset + node.length, found):
            return True
    return False


def match_at(flirt, buf):
    """buf: bytes starting exactly at the candidate function entry point."""
    found = []
    for child in flirt.root.children:
        if match_node(child, buf, 0, found):
            break
    return found


# ---------------- driver ----------------

def load_image(xbe_path):
    xbe = parse_xbe(xbe_path)
    data = xbe["data"]
    sections = xbe["sections"]

    def read_window(va, size):
        for s in sections:
            if s["vaddr"] <= va < s["vaddr"] + max(s["vsize"], s["raw_size"]):
                file_off = s["raw_offset"] + (va - s["vaddr"])
                avail = s["raw_offset"] + s["raw_size"] - file_off
                if avail <= 0:
                    return b""
                return data[file_off:file_off + min(size, avail)]
        return b""

    return read_window


def load_func_addrs(tsv_path, only_unnamed):
    addrs = []
    with open(tsv_path) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            start = int(parts[0], 16)
            end = int(parts[1], 16)
            is_default = parts[2] == "1"
            if only_unnamed and not is_default:
                continue
            addrs.append((start, end))
    return addrs


def find_sig_files(path):
    p = Path(path)
    if p.is_file():
        return [p]
    return sorted(p.rglob("*.sig"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xbe_path")
    ap.add_argument("func_addrs_tsv")
    ap.add_argument("sig_path")
    ap.add_argument("--out", required=True)
    ap.add_argument("--window", type=int, default=0x200, help="bytes to read per candidate address")
    ap.add_argument("--all-functions", action="store_true", help="also try already-named functions (default: only FUN_ default-named ones)")
    args = ap.parse_args()

    read_window = load_image(args.xbe_path)
    addrs = load_func_addrs(args.func_addrs_tsv, only_unnamed=not args.all_functions)
    sig_files = find_sig_files(args.sig_path)

    print(f"{len(addrs)} candidate addresses, {len(sig_files)} .sig files")

    results = {}  # addr -> (library, name)
    errors = 0
    for sig_file in sig_files:
        try:
            flirt = parse_flirt_file(sig_file)
        except Exception:
            errors += 1
            continue
        lib_name = sig_file.stem
        for start, _end in addrs:
            if start in results:
                continue
            buf = read_window(start, args.window)
            if not buf:
                continue
            found = match_at(flirt, buf)
            for func in found:
                if func.name and func.name != "?":
                    results[start] = (lib_name, func.name)
                    break

    with open(args.out, "w") as f:
        for addr, (lib, name) in sorted(results.items()):
            f.write(f"0x{addr:08x}\t{lib}\tFUN\t{name}\n")

    print(f"Matched {len(results)} addresses ({errors} .sig files failed to parse)")
    print(f"Wrote: {args.out}")


if __name__ == "__main__":
    main()
