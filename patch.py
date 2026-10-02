#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Timo Heimonen <timo.heimonen@proton.me>
"""Create <name>-fixed.ipf from the Populous (Amiga, Hit Squad v2.7) IPF image.

Fixes the roaming "funny" creatures.

The tree-planting wizard, the swamp monster and the rock monster are spawned
by ``_do_place_funny(type, edge)`` in ``populous.prg``.  ``edge`` picks the map
edge the creature starts on and matches its direction of travel (0 = south
edge for the northbound wizard, 1 = east/south edge for the north-west bound
swamp monster, 2 = north/west edge for the south-east bound rock monster).

Bug 1: both callers push only ``type``, so ``edge`` is whatever word happens
to sit above the argument on the caller's stack.  Both callers now pass
``edge = type``.

Bug 2: once edge 1 is really used, half of its starts are meant to be on the
south edge, but the position is computed as ``((r % 43) + 20) * 64 + 0xFC0``,
which lies off the 64x64 map (the spawn then writes past ``_map_who``).  The
stray shift is replaced with a NOP: row 63, columns 20..62.

Bug 3: the timed check (``_game_turn == 0x1000``) sits on the ``_animate``
path that also runs while the game is paused or in paint-map mode, where the
turn does not advance; every loop pass then spawns again and both special
slots fill up.  The check moves into ``funny_tick``, which runs right after
``_move_peeps`` only and replaces the unreferenced ``_mod_map``.

Bug 4: a settlement at or below its population threshold creates no walker,
but the code still ran the "table full" test (loop index == 208) with the
index never set on that path, so it compared stack residue.  In the original
that residue is the high word of a pointer into the data hunk; with the data
hunk at 0xD0xxxx the swamp monster appeared although the table was not full.
The test is now skipped unless the allocation loop has run.

The 68000 source of the patches is ``src/funny_fix.s``.  Every patch keeps the
size of the code it replaces and all calls are PC-relative, so no other code
moves and no hunk relocations change.

The IPF image is patched at the sector payloads: the affected AmigaDOS
sectors are re-encoded (odd/even split), the OFS data block checksum and the
MFM sector data checksum are recomputed and the IPF DATA record CRCs are
updated.  Only the image with SHA-256
82dd5dbb9e3690e14d1ac1e83831c217b37f6ba125fe67a233973d637101e1ce is accepted;
the input file is never modified.  Only the Python standard library is required.
"""

import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

# The only supported input: Populous, Hit Squad release, SPS/CAPS IPF image.
IPF_SHA256 = "82dd5dbb9e3690e14d1ac1e83831c217b37f6ba125fe67a233973d637101e1ce"
# The fixed image produced from it.
FIXED_IPF_SHA256 = "7b012d0f8b3e41216b63a6f9491f53557aee726e0c0b0b7cc30294ab11da9cf6"

PRG_NAME = "populous.prg"
PRG_SHA256 = "24aad78f9b4624c7434f1fd609910a7fff55a318a6741d823916108fa81ccb01"
ASM_SOURCE = Path(__file__).resolve().parent / "src" / "funny_fix.s"

# The code hunk data starts at file offset 0x28; Ghidra/analysis addresses use
# a load base of 0x10000 for the code hunk.
CODE_FILE_OFFSET = 0x28
CODE_BASE = 0x10000

PATCHES = [
    {
        "name": "_animate: run the turn check only after _move_peeps",
        "addr": 0x1076A,
        # jsr _move_peeps(PC)
        "orig": "4eba1c46",
        # jsr funny_tick(PC)   (funny_tick at 0x11074, see below)
        "new": "4eba0908",
    },
    {
        "name": "_animate: drop the old turn check from the shared (pause) path",
        "addr": 0x10834,
        # cmpi.w #$1000,_game_turn(A4) / bne.s +$10 / move.w _start_seed(A4),D0
        # and.w #3,D0 / move.w D0,-(SP) / jsr _do_place_funny(PC) / addq.w #2,SP
        "orig": "0c6c1000ae6a 6610 302c99e4 c07c0003 3f00 4eba5dc2 544f",
        # bra.s $1084c / nop x11
        "new": "6016" + " 4e71" * 11,
    },
    {
        "name": "funny_tick: _move_peeps, then the timed spawn (replaces unused _mod_map)",
        "addr": 0x11074,
        # first 30 bytes of _mod_map (never referenced)
        "orig": "4e550000 48e70f20 382d0008 607c 3a2d000a 606e 3005 ed40 3c00 dc44 41ec",
        # jsr _move_peeps(PC)
        # cmpi.w #$1000,_game_turn(A4) / bne.s .ret
        # moveq #3,D0 / and.w _start_seed(A4),D0 / move.w D0,-(SP) x2
        # jsr _do_place_funny(PC) / addq.w #4,SP
        # .ret: rts
        "new": "4eba133c 0c6c1000ae6a 6610 7003 c06c99e4 3f00 3f00 4eba557e 584f 4e75",
    },
    {
        "name": "_move_peeps: skip the table-full test when no walker was attempted",
        "addr": 0x12BB8,
        # ble.w $12d3a   (to the i == 208 test with i never set on this path)
        "orig": "6f000180",
        # ble.w $12d58   (past the test)
        "new": "6f00019e",
    },
    {
        "name": "entity-table-full trigger in _move_peeps",
        "addr": 0x12D48,
        # move.w #1,-(SP) / jsr _do_place_funny(PC) / addq.w #2,SP
        # move.w #1,_funny_done(A4)
        "orig": "3f3c0001 4eba38bc 544f 397c0001ae70",
        # moveq #1,D0 / move.w D0,_funny_done(A4) / move.w D0,-(SP) x2
        # jsr _do_place_funny(PC) / addq.w #4,SP
        "new": "7001 3940ae70 3f00 3f00 4eba38b6 584f",
    },
    {
        "name": "edge 1 south-edge position in _do_place_funny",
        "addr": 0x166F0,
        # add.w #20,D0 / asl.w #6,D0 / movea.l (SP)+,A0 / add.w #$fc0,D0
        # -> ((r % 43) + 20) * 64 + 0xFC0 = map index 5312..8000, off the 64x64 map
        "orig": "d07c0014 ed40 205f d07c0fc0",
        # add.w #20,D0 / nop / movea.l (SP)+,A0 / add.w #$fc0,D0
        # -> 0xFC0 + (r % 43) + 20 = row 63, columns 20..62 (south edge)
        "new": "d07c0014 4e71 205f d07c0fc0",
    },
]

SECTORS_PER_TRACK = 11
BLOCK_SIZE = 512
OFS_DATA_SIZE = 488
ROOT_BLOCK = 880
# Packed (odd/even split) layout of the 540-byte AmigaDOS sector payload in IPF.
SECTOR_PAYLOAD = 540
INFO_OFF, LABEL_OFF, HCSUM_OFF, DCSUM_OFF, DATA_OFF = 0, 4, 20, 24, 28


class PatchError(Exception):
    pass


# ---------------------------------------------------------------------------
# AmigaDOS MFM odd/even helpers.  IPF stores the data bits of each MFM word,
# so a long L is stored as oddbits(L) followed (block-wise) by evenbits(L).

def _compress(v):
    """Pack bits 0, 2, ..., 30 of a 32-bit value into a 16-bit value."""
    r = 0
    for i in range(16):
        r |= ((v >> (2 * i)) & 1) << i
    return r


def _spread(v):
    """Inverse of _compress."""
    r = 0
    for i in range(16):
        r |= ((v >> i) & 1) << (2 * i)
    return r


def decode_oddeven(buf):
    """Decode an odd/even packed area (odd half followed by even half)."""
    half = len(buf) // 2
    out = bytearray()
    for i in range(0, half, 2):
        odd = int.from_bytes(buf[i:i + 2], "big")
        even = int.from_bytes(buf[half + i:half + i + 2], "big")
        out += ((_spread(odd) << 1) | _spread(even)).to_bytes(4, "big")
    return bytes(out)


def encode_oddeven(data):
    odd = bytearray()
    even = bytearray()
    for i in range(0, len(data), 4):
        v = int.from_bytes(data[i:i + 4], "big")
        odd += _compress(v >> 1).to_bytes(2, "big")
        even += _compress(v).to_bytes(2, "big")
    return bytes(odd + even)


def mfm_checksum(packed):
    """AmigaDOS checksum (XOR of MFM longs AND 0x55555555) of a packed area."""
    x = 0
    for i in range(0, len(packed), 2):
        x ^= int.from_bytes(packed[i:i + 2], "big")
    return _spread(x)


# ---------------------------------------------------------------------------
# IPF container

class Sector:
    def __init__(self, record, payload_pos):
        self.record = record          # owning DATA record
        self.pos = payload_pos        # absolute file offset of the 540-byte payload

    def payload(self, img):
        return img[self.pos:self.pos + SECTOR_PAYLOAD]


class IPFImage:
    def __init__(self, raw):
        self.img = bytearray(raw)
        self.records = []
        self.sectors = {}             # (track, sector) -> Sector
        self._parse()

    def _u32(self, off):
        return struct.unpack_from(">I", self.img, off)[0]

    def _parse(self):
        img = self.img
        pos = 0
        imges = {}
        datas = {}
        while pos < len(img):
            name = bytes(img[pos:pos + 4]).decode("ascii", "replace")
            size = self._u32(pos + 4)
            rec = {"name": name, "pos": pos, "size": size, "extra": 0}
            if name == "IMGE":
                fields = struct.unpack_from(">17I", img, pos + 12)
                rec.update(cyl=fields[0], head=fields[1], blkcnt=fields[10], did=fields[13])
                imges[rec["did"]] = rec
            elif name == "DATA":
                dsize, _bsize, dcrc, did = struct.unpack_from(">4I", img, pos + 12)
                rec.update(extra=dsize, dcrc=dcrc, did=did)
                datas[did] = rec
            elif name not in ("CAPS", "INFO"):
                raise PatchError(f"unsupported IPF record {name!r} at {pos:#x}")
            self.records.append(rec)
            pos += size + rec["extra"]
        if bytes(img[0:4]) != b"CAPS":
            raise PatchError("not an IPF image")
        self.check_crcs()

        for did, im in imges.items():
            if im["blkcnt"] == 0:
                continue
            data = datas[did]
            area = data["pos"] + data["size"]
            for b in range(im["blkcnt"]):
                desc = struct.unpack_from(">8I", img, area + 32 * b)
                if desc[4] != 1:
                    raise PatchError("non-MFM block encoding")
                payload = self._find_payload(area + desc[7], area + data["extra"])
                if payload is None:
                    continue
                info = decode_oddeven(img[payload:payload + 4])
                fmt, trk, sec = info[0], info[1], info[2]
                if fmt != 0xFF or trk != im["cyl"] * 2 + im["head"]:
                    continue
                self.sectors[(trk, sec)] = Sector(data, payload)

    def _find_payload(self, off, end):
        """Return the offset of the 540-byte data element after a 4489 sync mark."""
        seen_sync = False
        while off < end:
            code = self.img[off]
            off += 1
            vc, kind = code >> 5, code & 0x1F
            count = int.from_bytes(self.img[off:off + vc], "big") if vc else 0
            off += vc
            if kind == 0:
                return None
            if kind == 1:
                seen_sync = bytes(self.img[off:off + count]) == b"\x44\x89\x44\x89"
            elif kind == 2 and seen_sync and count == SECTOR_PAYLOAD:
                return off
            if kind != 5:
                off += count
        return None

    def check_crcs(self):
        for rec in self.records:
            hdr = bytearray(self.img[rec["pos"]:rec["pos"] + rec["size"]])
            stored = struct.unpack_from(">I", hdr, 8)[0]
            hdr[8:12] = b"\0\0\0\0"
            if zlib.crc32(hdr) != stored:
                raise PatchError(f"{rec['name']} header CRC mismatch at {rec['pos']:#x}")
            if rec["name"] == "DATA" and rec["dcrc"]:
                start = rec["pos"] + rec["size"]
                if zlib.crc32(self.img[start:start + rec["extra"]]) != rec["dcrc"]:
                    raise PatchError(f"DATA area CRC mismatch at {rec['pos']:#x}")

    def update_data_crc(self, rec):
        start = rec["pos"] + rec["size"]
        if rec["dcrc"]:
            rec["dcrc"] = zlib.crc32(self.img[start:start + rec["extra"]])
            struct.pack_into(">I", self.img, rec["pos"] + 20, rec["dcrc"])
        struct.pack_into(">I", self.img, rec["pos"] + 8, 0)
        hcrc = zlib.crc32(self.img[rec["pos"]:rec["pos"] + rec["size"]])
        struct.pack_into(">I", self.img, rec["pos"] + 8, hcrc)

    # -- logical blocks ---------------------------------------------------

    def _sector(self, block):
        key = divmod(block, SECTORS_PER_TRACK)
        if key not in self.sectors:
            raise PatchError(f"block {block} (track {key[0]} sector {key[1]}) not found")
        return self.sectors[key]

    def read_block(self, block):
        p = self._sector(block).payload(self.img)
        if mfm_checksum(p[INFO_OFF:HCSUM_OFF]) != int.from_bytes(decode_oddeven(p[HCSUM_OFF:DCSUM_OFF]), "big"):
            raise PatchError(f"bad sector header checksum in block {block}")
        if mfm_checksum(p[DATA_OFF:]) != int.from_bytes(decode_oddeven(p[DCSUM_OFF:DATA_OFF]), "big"):
            raise PatchError(f"bad sector data checksum in block {block}")
        return decode_oddeven(p[DATA_OFF:])

    def write_block(self, block, data):
        assert len(data) == BLOCK_SIZE
        sec = self._sector(block)
        packed = encode_oddeven(data)
        csum = encode_oddeven(mfm_checksum(packed).to_bytes(4, "big"))
        self.img[sec.pos + DATA_OFF:sec.pos + SECTOR_PAYLOAD] = packed
        self.img[sec.pos + DCSUM_OFF:sec.pos + DATA_OFF] = csum
        self.update_data_crc(sec.record)

    def verify_all_sectors(self):
        for trk in range(160):
            for sec in range(SECTORS_PER_TRACK):
                self.read_block(trk * SECTORS_PER_TRACK + sec)


# ---------------------------------------------------------------------------
# OFS file system

def ofs_checksum_ok(block):
    return sum(struct.unpack(">128I", block)) & 0xFFFFFFFF == 0


def ofs_fix_checksum(block, off=20):
    block = bytearray(block)
    struct.pack_into(">I", block, off, 0)
    s = sum(struct.unpack(">128I", block)) & 0xFFFFFFFF
    struct.pack_into(">I", block, off, (-s) & 0xFFFFFFFF)
    return bytes(block)


def ofs_hash(name):
    h = len(name)
    for c in name.upper():
        h = (h * 13 + ord(c)) & 0x7FF
    return h % 72


def find_file(ipf, name):
    root = ipf.read_block(ROOT_BLOCK)
    if not ofs_checksum_ok(root) or struct.unpack_from(">I", root, 0)[0] != 2:
        raise PatchError("root block is not valid")
    key = struct.unpack_from(">I", root, 24 + 4 * ofs_hash(name))[0]
    while key:
        hdr = ipf.read_block(key)
        if not ofs_checksum_ok(hdr):
            raise PatchError(f"bad OFS checksum in header block {key}")
        hname = hdr[433:433 + hdr[432]].decode("latin-1")
        if hname.lower() == name.lower():
            return key, hdr
        key = struct.unpack_from(">I", hdr, 496)[0]
    raise PatchError(f"file {name!r} not found on disk")


def file_blocks(ipf, hdr_key, hdr):
    """Return the list of OFS data block numbers of a file and its size."""
    size = struct.unpack_from(">I", hdr, 324)[0]
    blocks = []
    key = struct.unpack_from(">I", hdr, 16)[0]
    while key:
        blk = ipf.read_block(key)
        typ, owner, seq, dsize, nxt = struct.unpack_from(">5I", blk, 0)
        if typ != 8 or owner != hdr_key or seq != len(blocks) + 1 or not ofs_checksum_ok(blk):
            raise PatchError(f"unexpected OFS data block {key}")
        blocks.append(key)
        key = nxt
    if len(blocks) != (size + OFS_DATA_SIZE - 1) // OFS_DATA_SIZE:
        raise PatchError("file block chain does not match file size")
    return blocks, size


def read_file(ipf, blocks, size):
    data = bytearray()
    for key in blocks:
        blk = ipf.read_block(key)
        dsize = struct.unpack_from(">I", blk, 12)[0]
        data += blk[24:24 + dsize]
    if len(data) != size:
        raise PatchError("file size mismatch")
    return bytes(data)


def write_file_bytes(ipf, blocks, file_off, new):
    """Overwrite bytes of a file in place, updating OFS and MFM checksums."""
    touched = {}
    for i, byte in enumerate(new):
        idx, rel = divmod(file_off + i, OFS_DATA_SIZE)
        key = blocks[idx]
        if key not in touched:
            touched[key] = bytearray(ipf.read_block(key))
        touched[key][24 + rel] = byte
    for key, blk in touched.items():
        ipf.write_block(key, ofs_fix_checksum(blk))
    return sorted(touched)


# ---------------------------------------------------------------------------

def apply_patches(prg):
    """Return (patched populous.prg, [(patch, file offset), ...])."""
    out = bytearray(prg)
    placed = []
    for p in PATCHES:
        orig = bytes.fromhex(p["orig"].replace(" ", ""))
        new = bytes.fromhex(p["new"].replace(" ", ""))
        if len(orig) != len(new):
            raise PatchError(f"{p['name']}: size mismatch")
        off = p["addr"] - CODE_BASE + CODE_FILE_OFFSET
        cur = bytes(out[off:off + len(orig)])
        if cur != orig:
            raise PatchError(f"{p['name']}: unexpected bytes {cur.hex()}")
        out[off:off + len(new)] = new
        placed.append((p, off))
    return bytes(out), placed


def build_fixed_image(raw, log=print):
    """Patch the original IPF image `raw`.

    Returns (fixed image, original populous.prg, patched populous.prg).
    """
    digest = hashlib.sha256(raw).hexdigest()
    if digest != IPF_SHA256:
        raise PatchError(f"input SHA-256 {digest} is not the supported Populous image "
                         f"({IPF_SHA256})")
    ipf = IPFImage(raw)
    ipf.verify_all_sectors()

    hdr_key, hdr = find_file(ipf, PRG_NAME)
    blocks, size = file_blocks(ipf, hdr_key, hdr)
    prg = read_file(ipf, blocks, size)
    if hashlib.sha256(prg).hexdigest() != PRG_SHA256:
        raise PatchError(f"{PRG_NAME} does not match the supported version")

    patched, placed = apply_patches(prg)
    for p, off in placed:
        n = len(bytes.fromhex(p["new"].replace(" ", "")))
        touched = write_file_bytes(ipf, blocks, off, patched[off:off + n])
        log(f"patched {p['name']}: {p['addr']:#x} (file {off:#x}, disk block(s) {touched})")

    # Re-parse the result from scratch and verify it end to end.
    check = IPFImage(bytes(ipf.img))
    check.verify_all_sectors()
    hdr_key2, hdr2 = find_file(check, PRG_NAME)
    blocks2, size2 = file_blocks(check, hdr_key2, hdr2)
    if read_file(check, blocks2, size2) != patched:
        raise PatchError("verification failed: patched file does not read back")
    fixed = bytes(check.img)
    if hashlib.sha256(fixed).hexdigest() != FIXED_IPF_SHA256:
        raise PatchError("verification failed: output SHA-256 differs from the expected image")
    return fixed, prg, patched


def parse_srec(text):
    """Return {address: byte} from Motorola S-records (S1/S2/S3 data only)."""
    data = {}
    for line in text.split():
        kind = line[1]
        if kind not in "123":
            continue
        alen = {"1": 2, "2": 3, "3": 4}[kind]
        rec = bytes.fromhex(line[2:])
        addr = int.from_bytes(rec[1:1 + alen], "big")
        for i, b in enumerate(rec[1 + alen:-1]):
            data[addr + i] = b
    return data


def check_asm_source(original_prg, patched_prg, asm=ASM_SOURCE, vasm="vasmm68k_mot"):
    """Assemble funny_fix.s and compare it with the patch table.

    Returns None if vasm is not installed.  Otherwise every byte the source
    defines must equal the patched program, and every changed byte must come
    from the source.
    """
    exe = shutil.which(vasm)
    if exe is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "funny_fix.srec"
        subprocess.run([exe, "-quiet", "-no-opt", "-Fsrec", "-s28", "-o", str(out), str(asm)],
                       check=True)
        src = parse_srec(out.read_text())

    def at(img, addr):
        return img[addr - CODE_BASE + CODE_FILE_OFFSET]

    for addr, b in src.items():
        if at(patched_prg, addr) != b:
            raise PatchError(f"{asm.name} differs from the patch table at {addr:#x}")
    for off in range(len(original_prg)):
        if original_prg[off] != patched_prg[off] and off - CODE_FILE_OFFSET + CODE_BASE not in src:
            raise PatchError(f"patched byte at file offset {off:#x} is not in {asm.name}")
    return len(src)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help=f"original IPF image (SHA-256 {IPF_SHA256})")
    ap.add_argument("-o", "--output",
                    help="output image (default: <input name>-fixed.ipf next to the input)")
    ap.add_argument("--extract-prg", metavar="PATH",
                    help=f"also write the patched {PRG_NAME} to PATH")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output) if args.output else src.with_name(src.stem + "-fixed.ipf")
    if dst.exists() and dst.resolve() == src.resolve():
        raise PatchError("refusing to overwrite the input image")

    raw = src.read_bytes()
    print(f"input:  {src}")
    print(f"        SHA-256 {hashlib.sha256(raw).hexdigest()}")
    fixed, original, patched = build_fixed_image(raw)

    checked = check_asm_source(original, patched)
    if checked is None:
        print("note:   vasmm68k_mot not found, src/funny_fix.s not cross-checked")
    else:
        print(f"source: src/funny_fix.s assembles to the same {checked} bytes")

    dst.write_bytes(fixed)
    print(f"output: {dst}")
    print(f"        SHA-256 {hashlib.sha256(fixed).hexdigest()}")
    if args.extract_prg:
        Path(args.extract_prg).write_bytes(patched)
        print(f"patched {PRG_NAME} written to {args.extract_prg}")


if __name__ == "__main__":
    try:
        main()
    except PatchError as e:
        sys.exit(f"error: {e}")
