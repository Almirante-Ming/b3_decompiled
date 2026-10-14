#!/usr/bin/env python3
"""
Recursively extract an Xbox XDVDFS partition from a raw disc image into a
local directory tree. Locates the partition itself by scanning for the
"MICROSOFT*XBOX*MEDIA" volume descriptor magic (works for a raw/full disc
dump that also carries the decoy DVD-Video/UDF partition ahead of it).

Usage: xdvdfs_extract.py <disc.iso> <out_dir> [--only PATH]
"""
import argparse
import os
import struct
import sys

MAGIC = b"MICROSOFT*XBOX*MEDIA"
SECTOR = 2048


def find_volume_lba0(f):
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    chunk_size = 64 * 1024 * 1024
    overlap = len(MAGIC) - 1
    pos = 0
    prev_tail = b""
    while pos < size:
        f.seek(pos)
        buf = prev_tail + f.read(chunk_size)
        idx = buf.find(MAGIC)
        if idx != -1:
            abs_off = pos - len(prev_tail) + idx
            return abs_off - 32 * SECTOR  # magic lives at sector 32 of the volume
        prev_tail = buf[-overlap:]
        pos += chunk_size
    raise ValueError("XDVDFS magic not found in this image")


def read_dir(f, vol_lba0, start_sector, size):
    f.seek(vol_lba0 + start_sector * SECTOR)
    data = f.read(size)
    entries = []
    off = 0
    while off < len(data):
        if off + 14 > len(data):
            break
        left, right, start, fsize, attr, namelen = struct.unpack_from("<HHIIBB", data, off)
        if left == 0xFFFF and right == 0xFFFF and start == 0xFFFFFFFF:
            break
        if namelen == 0 or namelen == 0xFF:
            break
        raw_name = data[off + 14:off + 14 + namelen]
        # Directory sectors aren't always fully zero-padded past the last
        # real entry; walking into that tail reads garbage as a bogus next
        # entry. A real XDVDFS name is plain ASCII, so anything outside
        # printable range means we've run off the end -- stop here rather
        # than crash trying to use it as a filename.
        if len(raw_name) < namelen or any(b < 0x20 or b > 0x7E for b in raw_name):
            break
        name = raw_name.decode("ascii")
        entries.append((name, start, fsize, attr))
        entry_len = 14 + namelen
        pad = (4 - (entry_len % 4)) % 4
        off += entry_len + pad
    return entries


def extract(f, vol_lba0, start_sector, size, dest_dir, rel_path, only_prefix, stats):
    entries = read_dir(f, vol_lba0, start_sector, size)
    for name, start, fsize, attr in entries:
        entry_rel = f"{rel_path}/{name}" if rel_path else name
        if only_prefix and not (entry_rel + "/").startswith(only_prefix) and not only_prefix.startswith(entry_rel + "/"):
            continue
        is_dir = bool(attr & 0x10)
        out_path = os.path.join(dest_dir, entry_rel)
        if is_dir:
            os.makedirs(out_path, exist_ok=True)
            extract(f, vol_lba0, start, fsize, dest_dir, entry_rel, only_prefix, stats)
        else:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            f.seek(vol_lba0 + start * SECTOR)
            remaining = fsize
            with open(out_path, "wb") as out:
                while remaining > 0:
                    chunk = f.read(min(remaining, 8 * 1024 * 1024))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            stats["files"] += 1
            stats["bytes"] += fsize
            print(f"  {entry_rel}  ({fsize:,} bytes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("iso_path")
    ap.add_argument("out_dir")
    ap.add_argument("--only", help="Only extract this path prefix (e.g. 'Data')")
    args = ap.parse_args()

    with open(args.iso_path, "rb") as f:
        print("Locating XDVDFS partition...")
        vol_lba0 = find_volume_lba0(f)
        print(f"Volume LBA0 at file offset {vol_lba0:,}")

        f.seek(vol_lba0 + 32 * SECTOR + 20)
        root_sector = struct.unpack("<I", f.read(4))[0]
        root_size = struct.unpack("<I", f.read(4))[0]

        os.makedirs(args.out_dir, exist_ok=True)
        stats = {"files": 0, "bytes": 0}
        extract(f, vol_lba0, root_sector, root_size, args.out_dir, "", args.only, stats)

        print(f"\nExtracted {stats['files']} files, {stats['bytes']:,} bytes total")


if __name__ == "__main__":
    main()
