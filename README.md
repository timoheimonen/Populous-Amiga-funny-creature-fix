# Populous (Amiga) – funny creature fix

A patch for the Amiga Populous IPF image ("Commodore_Amiga_TOSEC_2012_04_10" Hit Squad release, `Populous 2.7 17/3/89`)
that fixes the rarely seen roaming creatures: the tree-planting wizard, the
swamp monster and the rock monster.

## What was found

The game can spawn one of these creatures in two ways: at simulation turn
4096 (the type is taken from the world seed) or once per level when the
entity table is full (always the swamp monster). The spawn routine
`_do_place_funny(type, edge)` takes the map edge to start from as a second
argument, matching the creature's direction of travel. Four bugs break this:

1. **Missing argument.** Both callers push only `type`, so `edge` is whatever
   word is left on the stack. In the tested setup every timed creature started
   on the south edge, and the rock monster, which walks south-east, left the
   map on its first step. When the leftover word is not 0–2, the start
   position is not set at all and the creature appears where an earlier one
   used to be.
2. **Off-map start.** The south-edge start for the swamp monster is computed as
   `((r % 43) + 20) * 64 + 0xFC0`, a map index of 5312–8000 on a 64×64 map. The
   spawn then writes past the `_map_who` array. Bug 1 hides this bug.
3. **Repeat spawn while stopped.** The turn-4096 check also runs while the game
   is paused or in paint-map mode, when the turn does not advance, so every
   loop pass spawns again and a second creature appears.
4. **False "table full".** The table-full test also runs for settlements that
   do not try to create a walker, and then compares a loop index that was
   never set. The value is stack residue, the high word of a pointer, so on a
   machine where the game's data lands at `0xD0xxxx` the swamp monster appears
   although the table is not full.

## How it was fixed

Six patches to `populous.prg` (68000 source: [src/funny_fix.s](src/funny_fix.s)):

- Both callers pass `edge = type`.
- The stray shift in the swamp monster's south-edge start becomes a `nop`
  (row 63, columns 20–62).
- The timed check moves into a small routine that runs right after the
  simulation step only. It lives in the unused `_mod_map` routine.
- The table-full test runs only after the walker allocation loop.

Every patch keeps the size of the code it replaces, and all calls are
PC-relative, so nothing else moves. The disk image is patched in place: the
changed AmigaDOS sectors are re-encoded and the file system checksums, sector
checksums and IPF CRCs are recomputed.

## Usage

```sh
python3 patch.py Populous.ipf          # writes Populous-fixed.ipf next to the input
```

Only the image with SHA-256
`82dd5dbb9e3690e14d1ac1e83831c217b37f6ba125fe67a233973d637101e1ce` is accepted.
The result has SHA-256
`7b012d0f8b3e41216b63a6f9491f53557aee726e0c0b0b7cc30294ab11da9cf6`.
Python 3 is the only requirement. If `vasmm68k_mot` is installed, `patch.py` also
checks that `src/funny_fix.s` assembles to the same bytes.

The save game format is unchanged. In a two-player serial link game both
computers must use the same version: the game runs the simulation on both
machines in lockstep, and the fixed version spawns the creatures differently.

No game files are included. Populous is © Bullfrog Productions / Electronic Arts.

## License

MIT. © 2026 Timo Heimonen <timo.heimonen@proton.me>. See [LICENSE](LICENSE).
