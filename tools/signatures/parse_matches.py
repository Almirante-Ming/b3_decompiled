#!/usr/bin/env python3
"""
Parse XbSymbolDatabaseCLI's output (run with -e/-extendinfo) into a JSON map
of address -> {library, kind, name, signature}, for feeding into Ghidra
auto-renaming or a per-function resolution status table (SPEC.md Phase 1).

Usage:
    XbSymbolDatabaseCLI <xbe> -v -e > matches.txt
    parse_matches.py matches.txt --json matches.json
"""
import argparse
import json
import re
import sys

LINE_RE = re.compile(
    r"^(?P<lib>[A-Z0-9]+)__(?:(?P<kind>FUN|VAR)__)?(?P<rest>.+) = 0x(?P<addr>[0-9a-fA-F]+)$"
)


def parse(path):
    results = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            m = LINE_RE.match(line)
            if not m:
                continue
            addr = int(m.group("addr"), 16)
            kind = m.group("kind") or "VAR"
            rest = m.group("rest")
            name = rest
            calling_conv = None
            if kind == "FUN":
                # rest looks like "stdcall__FunctionName(args...)"
                parts = rest.split("__", 1)
                if len(parts) == 2:
                    calling_conv, name = parts
            results[f"0x{addr:08x}"] = {
                "library": m.group("lib"),
                "kind": kind,
                "name": name,
                "calling_convention": calling_conv,
                "raw": line,
            }
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("matches_txt")
    ap.add_argument("--json", required=True)
    ap.add_argument("--tsv", help="Also write address\\tlibrary\\tkind\\tname (for the Ghidra apply script)")
    args = ap.parse_args()

    results = parse(args.matches_txt)
    with open(args.json, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True)

    if args.tsv:
        with open(args.tsv, "w") as f:
            for addr, v in sorted(results.items()):
                f.write(f"{addr}\t{v['library']}\t{v['kind']}\t{v['name']}\n")

    by_lib = {}
    for v in results.values():
        by_lib.setdefault(v["library"], 0)
        by_lib[v["library"]] += 1

    print(f"Total resolved symbols: {len(results)}")
    for lib, count in sorted(by_lib.items(), key=lambda kv: -kv[1]):
        print(f"  {lib:12s} {count}")
    print(f"Wrote: {args.json}")


if __name__ == "__main__":
    sys.exit(main())
