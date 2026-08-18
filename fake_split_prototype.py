#!/usr/bin/env python3
"""
fake_split_prototype.py — Fake Split (Rig Split) PROTOTYPE for FT8/FT4.

⚠ THIS IS A SIMULATION PROTOTYPE — it does NOT touch the radio. It exists
to VERIFY the logic (does the resulting on-air frequency check out)
BEFORE wiring this into the TX chain.

═══ PROBLEM (transmit power below the set value, ALC fluctuates) ═══
The radio's SSB filter passes roughly 0-3000 Hz of audio, but rolls off
weakly near the edges (close to 0 Hz and 3000 Hz). If FT8 is transmitted
with an audio offset near the edge (e.g. 300 Hz or 2700 Hz), the filter
ATTENUATES the signal -> power drops, splatter/harmonics appear. The
cleanest point is ~1500 Hz (the center of the filter).

═══ SOLUTION (Fake Split) ═══
Instead of transmitting audio near the edge, we SHIFT the radio's VFO so
the audio ends up at ~1500 Hz, while the sum (VFO + audio) still lands on
EXACTLY the same on-air frequency. After transmitting, the VFO returns to
its base position (for RX).

KEY INVARIANT:
    on_air_freq = VFO_dial + audio_offset   (must stay CONSTANT before and after the split)

Example: you want to transmit at 14074000 + 2700 Hz audio = 14076700 Hz on air.
  Without split: VFO=14074000, audio=2700 (near the edge -> attenuation, low power)
  With split:    VFO=14075200, audio=1500 (center of the filter -> full power)
                 check: 14075200 + 1500 = 14076700 ✓ same frequency

IMPORTANT (safety): audio MUST stay within the filter passband (clamped to
a safe range), and the VFO must not go outside the amateur band (checked
by a higher layer — this module only computes the offsets).
"""

# Target center of the SSB filter (the cleanest point)
TARGET_AUDIO_HZ = 1500.0

# Safe audio range (within the filter passband, away from the edges)
AUDIO_MIN_HZ = 300.0
AUDIO_MAX_HZ = 2700.0

# The VFO is shifted in blocks (as in the original — 500 Hz), to avoid
# continuous CI-V tuning (the radio can't keep up with smooth changes).
# Block-wise = a handful of discrete steps.
VFO_STEP_HZ = 500.0


def compute_fake_split(dial_hz, desired_audio_hz):
    """
    Computes the Fake Split for a given audio frequency.

    Input:
      dial_hz          — the radio's current VFO (dial) frequency, Hz
      desired_audio_hz — the audio offset the user wants (where in the band to transmit)

    Returns a dict:
      on_air_hz     — the on-air frequency (invariant, must be preserved)
      split_needed  — whether a split is needed (audio near the edge?)
      new_dial_hz   — what frequency to set the VFO to (block-wise)
      new_audio_hz  — what audio offset to use (closer to 1500 Hz)
      restore_dial_hz — what to restore the VFO to after transmitting (= dial_hz)

    Invariant: new_dial_hz + new_audio_hz == on_air_hz (exactly).
    """
    on_air_hz = dial_hz + desired_audio_hz

    # Is the audio already in a safe zone? If so — no split needed.
    if AUDIO_MIN_HZ <= desired_audio_hz <= AUDIO_MAX_HZ:
        # Audio is already fine, but check whether it's worth nudging closer
        # to 1500. We only split when audio is near an edge (below ~600 or
        # above ~2400) — no point moving the VFO in the middle of the band.
        if 600.0 <= desired_audio_hz <= 2400.0:
            return {
                "on_air_hz": on_air_hz,
                "split_needed": False,
                "new_dial_hz": dial_hz,
                "new_audio_hz": desired_audio_hz,
                "restore_dial_hz": dial_hz,
            }

    # Split needed: we want audio at ~1500 Hz. Shift the VFO by the
    # difference, but BLOCK-WISE (a multiple of VFO_STEP_HZ) so CI-V can keep up.
    raw_shift = desired_audio_hz - TARGET_AUDIO_HZ  # how far audio is from 1500
    # Round the VFO shift to a 500 Hz block:
    vfo_shift = round(raw_shift / VFO_STEP_HZ) * VFO_STEP_HZ
    new_dial_hz = dial_hz + vfo_shift
    # audio = whatever's left so the sum preserves the invariant:
    new_audio_hz = on_air_hz - new_dial_hz

    return {
        "on_air_hz": on_air_hz,
        "split_needed": True,
        "new_dial_hz": new_dial_hz,
        "new_audio_hz": new_audio_hz,
        "restore_dial_hz": dial_hz,
    }


# ════════════════════════════════════════════════════════════════════════════
# SIMULATION / TESTS — proving the invariant holds
# ════════════════════════════════════════════════════════════════════════════
def _run_tests():
    passed = failed = 0

    def check(cond, name):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {name}")

    print("═══ Fake Split Simulation ═══\n")

    scenarios = [
        # (dial, desired_audio, description)
        (14074000, 2700, "FT8 20m, audio at the UPPER edge (2700)"),
        (14074000, 300,  "FT8 20m, audio at the LOWER edge (300)"),
        (14074000, 1500, "FT8 20m, audio already centered (1500) — split unnecessary"),
        (7074000,  2900, "FT8 40m, audio very close to the edge (2900)"),
        (14074000, 1000, "FT8 20m, audio 1000 (in-zone, split unnecessary)"),
        (3573000,  200,  "FT8 80m, audio very low (200)"),
    ]

    for dial, audio, desc in scenarios:
        r = compute_fake_split(dial, audio)
        # INVARIANT: the sum must equal the on-air frequency
        recomputed = r["new_dial_hz"] + r["new_audio_hz"]
        invariant_ok = abs(recomputed - r["on_air_hz"]) < 0.001
        check(invariant_ok, f"Frequency invariant: {desc}")

        print(f"  {desc}")
        print(f"    on air:    {r['on_air_hz']:.0f} Hz (dial {dial} + audio {audio})")
        if r["split_needed"]:
            print(f"    SPLIT:     VFO {dial} -> {r['new_dial_hz']:.0f}, "
                  f"audio {audio} -> {r['new_audio_hz']:.0f} Hz")
            print(f"    check:     {r['new_dial_hz']:.0f} + {r['new_audio_hz']:.0f} "
                  f"= {recomputed:.0f} Hz {'✓' if invariant_ok else '✗ ERROR!'}")
            print(f"    after TX, VFO returns to: {r['restore_dial_hz']:.0f}")
            # audio after the split should be closer to 1500 than the original
            closer = abs(r["new_audio_hz"] - 1500) <= abs(audio - 1500)
            check(closer, f"Audio closer to center: {desc}")
            print(f"    audio closer to center 1500: {'YES' if closer else 'NO'}")
        else:
            print(f"    split unnecessary (audio {audio} already in a good zone)")
        print()

    # Edge case: audio exactly 1500 -> split unnecessary, nothing changes
    r = compute_fake_split(14074000, 1500)
    check(not r["split_needed"], "Audio=1500 -> split unnecessary")
    check(r["new_dial_hz"] == 14074000, "Audio=1500 -> VFO unchanged")

    # Test: after the split, audio is ALWAYS within the filter's safe passband
    for dial, audio, _ in scenarios:
        r = compute_fake_split(dial, audio)
        if r["split_needed"]:
            in_band = AUDIO_MIN_HZ <= r["new_audio_hz"] <= AUDIO_MAX_HZ
            check(in_band, f"Audio in-band after split ({r['new_audio_hz']:.0f}Hz)")

    print("═" * 50)
    total = passed + failed
    if failed == 0:
        print(f"  RESULT: {passed}/{total} — LOGIC CORRECT ✓")
        print("  Frequency invariant holds. Audio always in-band.")
        print("  Safe to consider wiring into the TX chain.")
    else:
        print(f"  RESULT: {passed}/{total} — {failed} FAILURES ✗")
    print("═" * 50)
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run_tests() else 1)
