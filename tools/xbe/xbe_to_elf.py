#!/usr/bin/env python3
"""
Parse an original Xbox .xbe executable and optionally emit a synthetic
ELF32/x86 wrapper around its loadable sections, so generic tools (Ghidra,
IDA, etc.) can analyze it at the correct virtual addresses without native
XBE support.

Usage:
    xbe_to_elf.py default.xbe [--out-elf default.synthetic.elf] [--json sections.json]
"""
import argparse
import json
import struct
import sys

XOR_EP_RETAIL = 0xA8FC57AB
XOR_EP_DEBUG = 0x94859D4B
XOR_KT_RETAIL = 0x5B6D40B6
XOR_KT_DEBUG = 0x41561892

EHDR_SIZE = 52
PHDR_SIZE = 32


def u32(data, off):
    return struct.unpack_from("<I", data, off)[0]


def cstr_at_va(data, va, base):
    off = va - base
    end = data.index(b"\x00", off)
    return data[off:end].decode("latin1")


def parse_xbe(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"XBEH":
        raise ValueError(f"{path!r} is not an XBE (missing XBEH magic)")

    base = u32(data, 0x104)
    size_image = u32(data, 0x10C)
    cert_addr = u32(data, 0x118)
    num_sections = u32(data, 0x11C)
    sec_headers_addr = u32(data, 0x120)
    entry_raw = u32(data, 0x128)
    kt_addr_raw = u32(data, 0x158)

    entry_retail = entry_raw ^ XOR_EP_RETAIL
    entry_debug = entry_raw ^ XOR_EP_DEBUG
    is_debug = entry_retail > size_image
    entry_offset = entry_debug if is_debug else entry_retail
    kt_offset = (kt_addr_raw ^ XOR_KT_DEBUG) if is_debug else (kt_addr_raw ^ XOR_KT_RETAIL)

    cert_off = cert_addr - base
    title_id = u32(data, cert_off + 8)
    title_name = data[cert_off + 12:cert_off + 12 + 80].decode("utf-16-le", errors="replace").split("\x00")[0]

    sections = []
    for i in range(num_sections):
        off = (sec_headers_addr - base) + i * 56
        flags, vaddr, vsize, rawaddr, rawsize, name_addr = struct.unpack_from("<IIIIII", data, off)
        name = cstr_at_va(data, name_addr, base)
        sections.append({
            "name": name,
            "flags": flags,
            "writable": bool(flags & 0x1),
            "preload": bool(flags & 0x2),
            "executable": bool(flags & 0x4),
            "vaddr": vaddr,
            "vsize": vsize,
            "raw_offset": rawaddr,
            "raw_size": rawsize,
        })

    return {
        "data": data,
        "base": base,
        "size_image": size_image,
        "is_debug": is_debug,
        "entry_va": base + entry_offset,
        "kernel_thunk_table_va": base + kt_offset,
        "title_id": title_id,
        "title_name": title_name,
        "sections": sections,
    }


def build_synthetic_elf(xbe):
    data = xbe["data"]
    loadable = [s for s in xbe["sections"] if s["raw_size"] > 0]

    phoff = EHDR_SIZE
    data_start = phoff + len(loadable) * PHDR_SIZE

    elf = bytearray()
    elf += b"\x7fELF"
    elf += bytes([1, 1, 1, 0]) + b"\x00" * 8  # ELFCLASS32, ELFDATA2LSB, EV_CURRENT, ELFOSABI_NONE
    elf += struct.pack(
        "<HHIIIIIHHHHHH",
        2,                   # e_type ET_EXEC
        3,                   # e_machine EM_386
        1,                   # e_version
        xbe["entry_va"],     # e_entry
        phoff,               # e_phoff
        0, 0,                # e_shoff, e_flags
        EHDR_SIZE, PHDR_SIZE, len(loadable),
        0, 0, 0,             # e_shentsize, e_shnum, e_shstrndx
    )
    assert len(elf) == EHDR_SIZE

    cur_off = data_start
    payloads = []
    for s in loadable:
        p_flags = 4  # PF_R
        if s["writable"]:
            p_flags |= 2
        if s["executable"]:
            p_flags |= 1
        payload = data[s["raw_offset"]:s["raw_offset"] + s["raw_size"]]
        elf += struct.pack(
            "<IIIIIIII",
            1,              # p_type PT_LOAD
            cur_off,        # p_offset
            s["vaddr"], s["vaddr"],
            s["raw_size"], s["vsize"],
            p_flags, 0x1000,
        )
        payloads.append((cur_off, payload))
        cur_off += len(payload)

    body = bytearray(cur_off - data_start)
    for p_offset, payload in payloads:
        body[p_offset - data_start:p_offset - data_start + len(payload)] = payload
    elf += body
    return bytes(elf)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xbe_path")
    ap.add_argument("--out-elf", help="Write a synthetic ELF32/x86 wrapper for analysis tools")
    ap.add_argument("--json", help="Write parsed header/section metadata as JSON")
    args = ap.parse_args()

    xbe = parse_xbe(args.xbe_path)

    print(f"Base address        : 0x{xbe['base']:08X}")
    print(f"Size of image (virt): {xbe['size_image']:,} bytes")
    print(f"Build type           : {'DEBUG' if xbe['is_debug'] else 'RETAIL'}")
    print(f"Entry point VA        : 0x{xbe['entry_va']:08X}")
    print(f"Kernel thunk table VA : 0x{xbe['kernel_thunk_table_va']:08X}")
    print(f"Title ID               : 0x{xbe['title_id']:08X}")
    print(f"Title name              : {xbe['title_name']!r}")
    print(f"\n{'Name':15s} {'VA':>10s} {'VSize':>10s} {'RawOff':>10s} {'RawSize':>10s} {'Flags':>6s}")
    for s in xbe["sections"]:
        flag_str = ("W" if s["writable"] else "") + ("P" if s["preload"] else "") + ("X" if s["executable"] else "")
        print(f"{s['name']:15s} 0x{s['vaddr']:08X} 0x{s['vsize']:08X} 0x{s['raw_offset']:08X} 0x{s['raw_size']:08X} {flag_str:>6s}")

    if args.json:
        out = {k: v for k, v in xbe.items() if k != "data"}
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nWrote metadata: {args.json}")

    if args.out_elf:
        elf_bytes = build_synthetic_elf(xbe)
        with open(args.out_elf, "wb") as f:
            f.write(elf_bytes)
        print(f"\nWrote synthetic ELF: {args.out_elf} ({len(elf_bytes):,} bytes)")


if __name__ == "__main__":
    sys.exit(main())
