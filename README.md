# Populous (Amiga) funny creature fix

Populous has three creatures that hardly anyone ever saw: the wizard who
plants trees, the swamp monster and the rock monster. The game code calls them
"funny" (`_do_place_funny`). In the Amiga version they are mostly broken, and
this patch fixes them.

It is made for one specific disk image, the Hit Squad release from the TOSEC
set "Commodore_Amiga_TOSEC_2012_04_10" (`Populous 2.7 17/3/89`).

## The bugs

A creature can show up in two ways. At simulation turn 4096 the game picks one
based on the world seed, and once per level it spawns a swamp monster if the
walker table is full. Both go through `_do_place_funny(type, edge)`, where
`edge` is the map edge the creature starts from. Each creature walks in a fixed
direction, so the edge has to match it.

The callers only push `type`, never `edge`, so the routine reads whatever word
happens to be left on the stack. In my testing the timed creature always
started on the south edge, which meant the rock monster (it walks south-east)
left the map on its first step. If the leftover word isn't 0–2, the start
position isn't set at all and the creature appears where an earlier one used
to be.

The swamp monster's south-edge start is wrong too. It is calculated as
`((r % 43) + 20) * 64 + 0xFC0`, which gives a map index of 5312–8000 on a
64×64 map, and the spawn writes past the end of `_map_who`. You never run into
this because of the first bug.

The turn-4096 check also runs while the game is paused or in paint-map mode.
The turn counter doesn't move then, so the spawn fires again on every pass of
the main loop and you get a second creature.

The table-full check runs even for settlements that aren't trying to create a
walker, and in that case it compares a loop index that was never set. What it
really compares is stack garbage, the high word of a pointer. If the game's
data happens to be loaded at `0xD0xxxx`, the swamp monster appears even though
the table isn't full.

## The fix

Six patches to `populous.prg`. The 68000 source is in
[src/funny_fix.s](src/funny_fix.s).

- Both callers now pass `edge = type`.
- A stray shift in the swamp monster's south-edge calculation is replaced with
  a `nop`, so it starts on row 63, columns 20–62.
- The timed check is moved into a small routine that only runs right after a
  simulation step. It sits in the space of `_mod_map`, which isn't used.
- The table-full check only runs after the walker allocation loop.

None of the patches change the size of the code they replace, and all calls
are PC-relative, so nothing else in the program moves. `patch.py` edits the
disk image in place: it re-encodes the changed AmigaDOS sectors and
recalculates the file system checksums, sector checksums and IPF CRCs.

## Usage

```sh
python3 patch.py Populous.ipf
```

This writes `Populous-fixed.ipf` next to the input file. You only need
Python 3. If `vasmm68k_mot` is installed, the script also checks that
`src/funny_fix.s` assembles to the same bytes.

The script only accepts the image with SHA-256
`82dd5dbb9e3690e14d1ac1e83831c217b37f6ba125fe67a233973d637101e1ce`.
The patched image should have SHA-256
`7b012d0f8b3e41216b63a6f9491f53557aee726e0c0b0b7cc30294ab11da9cf6`.

Save games work as before. For a two-player game over the serial link, both
computers need the same version. The simulation runs in lockstep on both
machines and the fixed version spawns the creatures differently, so mixing
versions won't stay in sync.

No game files are included. Populous is © Bullfrog Productions / Electronic Arts.

## License

MIT. © 2026 Timo Heimonen <timo.heimonen@proton.me>. See [LICENSE](LICENSE).
