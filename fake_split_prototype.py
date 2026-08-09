#!/usr/bin/env python3
"""
fake_split_prototype.py — PROTOTYP Fake Split (Rig Split) dla FT8/FT4.

⚠ TO JEST PROTOTYP Z SYMULACJA — NIE dotyka radia. Sluzy do WERYFIKACJI
logiki (czy czestotliwosc w eterze sie zgadza) ZANIM wepniemy to w tor TX.

═══ PROBLEM (obserwacja Toma: moc < ustawiona, ALC skacze) ═══
Filtr SSB radia przepuszcza ~0-3000 Hz audio, ale ma SLABE zbocza przy
krawedziach (blisko 0 Hz i 3000 Hz). Jesli nadajesz FT8 z offsetem audio
blisko krawedzi (np. 300 Hz albo 2700 Hz), filtr TLUMI sygnal -> moc spada,
pojawiaja sie splattery/harmoniczne. Najczystsze jest ~1500 Hz (srodek filtra).

═══ ROZWIAZANIE (Fake Split) ═══
Zamiast nadawac audio blisko krawedzi, PRZESUWAMY VFO radia tak, zeby audio
bylo ~1500 Hz, a suma (VFO + audio) dawala DOKLADNIE te sama czestotliwosc
w eterze. Po nadaniu VFO wraca na pozycje bazowa (dla RX).

KLUCZOWA ZASADA (niezmiennik):
    freq_eteru = VFO_dial + audio_offset   (musi byc STALA przed i po split)

Przyklad: chcesz nadac na 14074000 + 2700 Hz audio = 14076700 Hz w eterze.
  Bez split: VFO=14074000, audio=2700 (blisko krawedzi -> tlumienie, mala moc)
  Ze split:  VFO=14075200, audio=1500 (srodek filtra -> pelna moc)
             sprawdzenie: 14075200 + 1500 = 14076700 ✓ ta sama czestotliwosc

WAZNE (bezpieczenstwo): audio MUSI zostac w pasmie filtra (klamrujemy do
bezpiecznego zakresu), a VFO nie moze wyjsc poza pasmo amatorskie (to
sprawdza wyzsza warstwa — tu tylko liczymy offsety).
"""

# Docelowy srodek filtra SSB (najczystszy punkt)
TARGET_AUDIO_HZ = 1500.0

# Bezpieczny zakres audio (w pasmie filtra, z dala od krawedzi)
AUDIO_MIN_HZ = 300.0
AUDIO_MAX_HZ = 2700.0

# VFO przesuwamy blokami (jak oryginal — 500 Hz), zeby uniknac ciaglego
# strojenia CI-V (radio nie nadaza na plynne zmiany). Blokowe = kilka
# dyskretnych krokow.
VFO_STEP_HZ = 500.0


def compute_fake_split(dial_hz, desired_audio_hz):
    """
    Oblicza Fake Split dla zadanej czestotliwosci audio.

    Wejscie:
      dial_hz          — aktualna czestotliwosc VFO (dial) radia, Hz
      desired_audio_hz — offset audio ktory user chce (gdzie w pasmie nadac)

    Zwraca dict:
      on_air_hz     — czestotliwosc w eterze (niezmiennik, ma byc zachowana)
      split_needed  — czy split jest potrzebny (audio blisko krawedzi?)
      new_dial_hz   — na jaka czestotliwosc ustawic VFO (blokowo)
      new_audio_hz  — jaki offset audio uzyc (blizej 1500 Hz)
      restore_dial_hz — na co przywrocic VFO po nadaniu (= dial_hz)

    Niezmiennik: new_dial_hz + new_audio_hz == on_air_hz (dokladnie).
    """
    on_air_hz = dial_hz + desired_audio_hz

    # Czy audio jest w bezpiecznej strefie? Jesli tak — split zbedny.
    if AUDIO_MIN_HZ <= desired_audio_hz <= AUDIO_MAX_HZ:
        # Audio juz w dobrym miejscu, ale sprawdzmy czy warto przyblizyc do 1500.
        # Robimy split tylko gdy audio jest blisko krawedzi (ponizej ~600 lub
        # powyzej ~2400) — w srodku pasma nie ma sensu ruszac VFO.
        if 600.0 <= desired_audio_hz <= 2400.0:
            return {
                "on_air_hz": on_air_hz,
                "split_needed": False,
                "new_dial_hz": dial_hz,
                "new_audio_hz": desired_audio_hz,
                "restore_dial_hz": dial_hz,
            }

    # Split potrzebny: chcemy audio ~1500 Hz. Przesuwamy VFO o roznice,
    # ale BLOKOWO (wielokrotnosc VFO_STEP_HZ), zeby CI-V nadazyl.
    raw_shift = desired_audio_hz - TARGET_AUDIO_HZ  # ile audio jest od 1500
    # Zaokraglij przesuniecie VFO do bloku 500 Hz:
    vfo_shift = round(raw_shift / VFO_STEP_HZ) * VFO_STEP_HZ
    new_dial_hz = dial_hz + vfo_shift
    # audio = to co zostaje, zeby suma dala niezmiennik:
    new_audio_hz = on_air_hz - new_dial_hz

    return {
        "on_air_hz": on_air_hz,
        "split_needed": True,
        "new_dial_hz": new_dial_hz,
        "new_audio_hz": new_audio_hz,
        "restore_dial_hz": dial_hz,
    }


# ════════════════════════════════════════════════════════════════════════════
# SYMULACJA / TESTY — udowadniamy ze niezmiennik jest zachowany
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

    print("═══ Symulacja Fake Split ═══\n")

    scenarios = [
        # (dial, desired_audio, opis)
        (14074000, 2700, "FT8 20m, audio przy GORNEJ krawedzi (2700)"),
        (14074000, 300,  "FT8 20m, audio przy DOLNEJ krawedzi (300)"),
        (14074000, 1500, "FT8 20m, audio juz w srodku (1500) — split zbedny"),
        (7074000,  2900, "FT8 40m, audio bardzo blisko krawedzi (2900)"),
        (14074000, 1000, "FT8 20m, audio 1000 (w strefie, split zbedny)"),
        (3573000,  200,  "FT8 80m, audio bardzo nisko (200)"),
    ]

    for dial, audio, desc in scenarios:
        r = compute_fake_split(dial, audio)
        # NIEZMIENNIK: suma musi rownac sie czestotliwosci w eterze
        recomputed = r["new_dial_hz"] + r["new_audio_hz"]
        invariant_ok = abs(recomputed - r["on_air_hz"]) < 0.001
        check(invariant_ok, f"Niezmiennik freq: {desc}")

        print(f"  {desc}")
        print(f"    eter:      {r['on_air_hz']:.0f} Hz (dial {dial} + audio {audio})")
        if r["split_needed"]:
            print(f"    SPLIT:     VFO {dial} -> {r['new_dial_hz']:.0f}, "
                  f"audio {audio} -> {r['new_audio_hz']:.0f} Hz")
            print(f"    sprawdzam: {r['new_dial_hz']:.0f} + {r['new_audio_hz']:.0f} "
                  f"= {recomputed:.0f} Hz {'✓' if invariant_ok else '✗ BLAD!'}")
            print(f"    po TX VFO wraca na: {r['restore_dial_hz']:.0f}")
            # audio po splicie powinno byc blizej 1500 niz oryginal
            closer = abs(r["new_audio_hz"] - 1500) <= abs(audio - 1500)
            check(closer, f"Audio blizej srodka: {desc}")
            print(f"    audio blizej srodka 1500: {'TAK' if closer else 'NIE'}")
        else:
            print(f"    split zbedny (audio {audio} juz w dobrej strefie)")
        print()

    # Test brzegowy: audio dokladnie 1500 -> split zbedny, nic sie nie zmienia
    r = compute_fake_split(14074000, 1500)
    check(not r["split_needed"], "Audio=1500 -> split zbedny")
    check(r["new_dial_hz"] == 14074000, "Audio=1500 -> VFO bez zmian")

    # Test: po splicie audio ZAWSZE w bezpiecznym pasmie filtra
    for dial, audio, _ in scenarios:
        r = compute_fake_split(dial, audio)
        if r["split_needed"]:
            in_band = AUDIO_MIN_HZ <= r["new_audio_hz"] <= AUDIO_MAX_HZ
            check(in_band, f"Audio po splicie w pasmie ({r['new_audio_hz']:.0f}Hz)")

    print("═" * 50)
    total = passed + failed
    if failed == 0:
        print(f"  WYNIK: {passed}/{total} — LOGIKA POPRAWNA ✓")
        print("  Niezmiennik czestotliwosci zachowany. Audio zawsze w pasmie.")
        print("  Mozna rozwazyc wpiecie w tor TX (za zgoda Toma).")
    else:
        print(f"  WYNIK: {passed}/{total} — {failed} BLEDOW ✗")
    print("═" * 50)
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run_tests() else 1)
