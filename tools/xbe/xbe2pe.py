import struct, sys

xbe = open(sys.argv[1], 'rb').read()
assert xbe[:4] == b'XBEH', xbe[:4]
base,          = struct.unpack_from('<I', xbe, 0x104)   # base address
nsec,          = struct.unpack_from('<I', xbe, 0x11C)
sec_hdr_addr,  = struct.unpack_from('<I', xbe, 0x120)
entry_enc,     = struct.unpack_from('<I', xbe, 0x128)

secs = []
off = sec_hdr_addr - base
for i in range(nsec):
    o = off + i * 0x38
    flags, va, vsize, raw, rsize, name_addr = struct.unpack_from('<IIIIII', xbe, o)
    nm = b''
    p = name_addr - base
    if 0 < p < len(xbe):
        e = xbe.index(b'\0', p)
        nm = xbe[p:e][:8]
    secs.append((nm or b'sec%d' % i, va, vsize, raw, rsize, flags))

# Entry point: XOR-encoded, two known keys (retail / debug).
ep = 0
for key in (0xA8FC57AB, 0x94859D4B):
    cand = entry_enc ^ key
    if base <= cand < base + 0x1000000:
        ep = cand; break

FA, SA = 0x200, 0x1000
nsh = len(secs)
hdr_size = 0x40 + 0xF8 + nsh * 0x28
hdr_size = (hdr_size + FA - 1) // FA * FA

out = bytearray()
out += b'MZ' + b'\0' * 0x3a + struct.pack('<I', 0x40)
out += b'PE\0\0'
out += struct.pack('<HHIIIHH', 0x14C, nsh, 0, 0, 0, 0xE0, 0x0102)

size_of_image = 0
for _, va, vsize, _, _, _ in secs:
    size_of_image = max(size_of_image, (va - base) + vsize)
size_of_image = (size_of_image + SA - 1) // SA * SA

opt = struct.pack('<HBBIIIIIII', 0x10B, 0, 0, 0, 0, 0, ep - base, 0, 0, base)
opt += struct.pack('<IIHHHHHHIIIIHH', SA, FA, 4, 0, 0, 0, 4, 0,
                   size_of_image, hdr_size, 0, 2, 0, 0)
opt += struct.pack('<IIIIII', 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
opt += b'\0' * (16 * 8)
out += opt
assert len(out) == 0x40 + 4 + 20 + 0xE0, len(out)

file_off = hdr_size
tbl = bytearray()
body = bytearray()
for nm, va, vsize, raw, rsize, flags in secs:
    rs = (rsize + FA - 1) // FA * FA
    ch = 0x40000000 | 0x00000040           # read | initialised data
    if flags & 0x00000004: ch |= 0x20000000 | 0x00000020   # execute | code
    if flags & 0x00000001: ch |= 0x80000000                # write
    tbl += nm.ljust(8, b'\0')
    tbl += struct.pack('<IIIIIIHHI', vsize, va - base, rs, file_off, 0, 0, 0, 0, ch)
    body += xbe[raw:raw + rsize].ljust(rs, b'\0')
    file_off += rs

out += tbl
out += b'\0' * (hdr_size - len(out))
out += body
open(sys.argv[2], 'wb').write(out)
print(f"base=0x{base:08X} entry=0x{ep:08X} secoes={nsh} saida={len(out):,} bytes")
for nm, va, vsize, raw, rsize, flags in secs[:4]:
    print(f"  {nm.decode(errors='replace'):10s} VA=0x{va:08X} vsize={vsize}")
