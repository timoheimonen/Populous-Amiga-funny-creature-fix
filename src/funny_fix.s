; SPDX-License-Identifier: MIT
; Copyright (c) 2026 Timo Heimonen <timo.heimonen@proton.me>
;
; Populous (Amiga, Hit Squad release "Populous 2.7 17/3/89"), populous.prg:
; fixes for the roaming "funny" creatures (wizard, swamp monster, rock monster).
;
; Addresses are analysis addresses: the code hunk loaded at $10000, A4 is the
; Aztec C small-data base.  Every block replaces code of the same size and
; all calls are PC-relative, so no hunk relocations change.
;
; Assemble (patch.py does this to verify its patch bytes):
;   vasmm68k_mot -quiet -no-opt -Fsrec -s28 -o funny_fix.srec funny_fix.s

; populous.prg routines
_move_peeps         equ $123b2
_do_place_funny     equ $1660a          ; (type.w, edge.w)
show_the_shield_call equ $1084c         ; jsr _show_the_shield in _animate
settlement_done     equ $12d58          ; _move_peeps, after the "table full" test

; A4-relative globals
_game_turn          equ -$5196
_funny_done         equ -$5190
_start_seed         equ -$661c

TIMED_TURN          equ $1000


; ---------------------------------------------------------------------------
; Bug 1 + 3, _animate: call funny_tick instead of _move_peeps on the running
; (not paused, not paint-map) path.
        org     $1076a
        jsr     funny_tick(pc)          ; was: jsr _move_peeps(pc)


; ---------------------------------------------------------------------------
; Bug 3, _animate: the old timed check sat on the path shared with pause and
; paint-map mode, where the turn does not advance, so it fired on every pass.
; It now lives in funny_tick; skip the old 24 bytes.
        org     $10834
        bra.s   show_the_shield_call
        rept    11
        nop
        endr


; ---------------------------------------------------------------------------
; Bug 1 + 3: funny_tick replaces the first 30 bytes of _mod_map, which is
; never referenced.  It runs once per simulation step, right after the turn
; counter has advanced, and passes edge = type (the original pushed only type,
; so edge was stale stack data).
        org     $11074
funny_tick:
        jsr     _move_peeps(pc)
        cmpi.w  #TIMED_TURN,_game_turn(a4)
        bne.s   .done
        moveq   #3,d0
        and.w   _start_seed(a4),d0      ; type = start seed & 3
        move.w  d0,-(sp)                ; edge = type
        move.w  d0,-(sp)                ; type
        jsr     _do_place_funny(pc)
        addq.w  #4,sp
.done:
        rts


; ---------------------------------------------------------------------------
; Bug 4, _move_peeps: a settlement at or below its population threshold
; creates no walker, but the code still went on to the "table full" test
; (i == 208) with the loop index i = -4(a5) never set in that case.  i then
; held stack residue (in the original: the high word of a pointer left by
; _do_funny, $00c2 with the data hunk at $c2xxxx), so with the data hunk at
; $d0xxxx the swamp monster appeared although the table was not full.  Skip
; the test unless the allocation loop has run.
        org     $12bb8
        ble.w   settlement_done         ; was: ble.w table_full_test ($12d3a)


; ---------------------------------------------------------------------------
; Bug 1, _move_peeps: no free entity in 0..207, spawn the swamp monster once
; per level.  Pass edge = type = 1.  _funny_done is set before the call,
; which is equivalent because _do_place_funny never reads it.
        org     $12d48
        moveq   #1,d0
        move.w  d0,_funny_done(a4)
        move.w  d0,-(sp)                ; edge = 1
        move.w  d0,-(sp)                ; type = 1 (swamp monster)
        jsr     _do_place_funny(pc)
        addq.w  #4,sp


; ---------------------------------------------------------------------------
; Bug 2, _do_place_funny: the south-edge start for edge 1 was computed as
; ((r % 43) + 20) * 64 + $fc0, an index past the 64x64 map.  Dropping the
; shift gives $fc0 + (r % 43) + 20: row 63, columns 20..62.
        org     $166f4
        nop                             ; was: asl.w #6,d0
