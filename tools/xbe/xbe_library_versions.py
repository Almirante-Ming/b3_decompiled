#!/usr/bin/env python3
"""
Dump the XBE library-versions table: every statically linked library the
title declares (name + XDK build number), plus the dedicated kernel/XAPI
library version entries.

Usage: xbe_library_versions.py default.xbe
"""
import struct
import sys


def u32(data, off):
    return struct.unpack_from("<I", data, off)[0]


def read_lib_version(data, off):
    name = data[off:off + 8].rstrip(b"\x00").decode("latin1")
    major, minor, build, flags = struct.unpack_from("<HHHH", data, off + 8)
    qfe = flags & 0x1FFF
    approved = (flags >> 13) & 0x3
    debug = bool((flags >> 15) & 0x1)
    return name, major, minor, build, qfe, approved, debug


def main():
    path = sys.argv[1]
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"XBEH"

    base = u32(data, 0x104)
    count = u32(data, 0x160)
    table_va = u32(data, 0x164)
    kernel_va = u32(data, 0x168)
    xapi_va = u32(data, 0x16C)

    print(f"{count} declared library versions:\n")
    print(f"{'name':10s} {'ver':>10s} {'qfe':>5s} {'approved':>9s} {'debug':>6s}")
    off = table_va - base
    for i in range(count):
        name, major, minor, build, qfe, approved, debug = read_lib_version(data, off + i * 16)
        print(f"{name:10s} {major}.{minor}.{build:>6} {qfe:>5} {approved:>9} {str(debug):>6}")

    print()
    name, major, minor, build, qfe, approved, debug = read_lib_version(data, kernel_va - base)
    print(f"Kernel library version : {name} {major}.{minor}.{build} qfe={qfe}")
    name, major, minor, build, qfe, approved, debug = read_lib_version(data, xapi_va - base)
    print(f"XAPI library version   : {name} {major}.{minor}.{build} qfe={qfe}")


if __name__ == "__main__":
    main()
