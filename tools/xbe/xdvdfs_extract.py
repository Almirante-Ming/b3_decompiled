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
    """Entries of one directory, by walking its tree.

    An XDVDFS directory is a binary tree, not a list: each entry carries the
    offsets of its left and right subtrees (in 4-byte units, from the start of
    the directory data, 0 meaning no child). Entries do not cross a sector
    boundary, so the tail of a sector is padded -- which is exactly why a
    linear walk cannot work. Reading these in address order and stopping at
    the first padding byte silently truncated every directory whose entries
    span more than one sector: `tracks` came out with 76 of its entries and
    without tlist.bin, and the title, finding its track list missing,
    reported the disc as dirty.
    """
    f.seek(vol_lba0 + start_sector * SECTOR)
    data = f.read(size)
    entries = []
    # Explicit stack over subtree offsets, plus a visited set: a corrupt or
    # misread image can point a subtree at itself, and recursion on a tree
    # read out of untrusted bytes should not be able to hang the extractor.
    stack, seen = [0], set()
    while stack:
        off = stack.pop()
        if off in seen or off + 14 > len(data):
            continue
        seen.add(off)
        left, right, start, fsize, attr, namelen = struct.unpack_from("<HHIIBB", data, off)
        # 0xFFFF subtree offsets and an all-ones entry are both end markers.
        if left == 0xFFFF and right == 0xFFFF and start == 0xFFFFFFFF:
            continue
        if namelen == 0 or namelen == 0xFF:
            continue
        raw_name = data[off + 14:off + 14 + namelen]
        # A real name is plain ASCII. Anything else means this offset is not
        # an entry -- abandon the branch, but keep walking the others, which
        # is the difference between losing one subtree and losing the rest of
        # the directory.
        if len(raw_name) < namelen or any(b < 0x20 or b > 0x7E for b in raw_name):
            continue
        entries.append((raw_name.decode("ascii"), start, fsize, attr))
        if left and left != 0xFFFF:
            stack.append(left * 4)
        if right and right != 0xFFFF:
            stack.append(right * 4)
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
            # Already there at exactly the recorded length: leave it. Makes a
            # re-run after fixing the directory walk cost only the files that
            # were actually missing, instead of rewriting the other ~1.9 GB --
            # and re-running is the normal way this tool gets used, because a
            # parsing bug is only ever found after an extraction.
            if os.path.exists(out_path) and os.path.getsize(out_path) == fsize:
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
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
