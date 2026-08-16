# -*- coding: utf-8 -*-
"""Test warstwy jezykowej dekodera CW (deepcw_lang.py).

Uruchamiac jako skrypt (nie pytest): python test_deepcw_lang.py
Windows: PYTHONIOENCODING=utf-8 python test_deepcw_lang.py (polskie znaki w konsoli).
"""
import deepcw_lang as L

passed = 0
failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  BLAD: {label}")


print("=== _sub_cost: Morse-bliskie znaki tanie, Morse-dalekie drogie ===")
# E "." vs T "-" -> 1 symbol roznicy -> koszt 1 (jak dawna plaska odleglosc)
check("E<->T koszt 1 (morse-bliskie)", L._sub_cost("E", "T") == 1)
# O "---" vs T "-" -> 2 dodane kreski -> koszt 2 (dawniej flat=1, teraz drozej)
check("O<->T koszt >1 (morse-dalekie)", L._sub_cost("O", "T") > 1)
check("sam ze soba koszt 0", L._sub_cost("K", "K") == 0)

print("\n=== _best_match: morse-bliska pomylka nadal sie poprawia ===")
# TNX: T=- N=-. X=-..-. Pomylka E<->T (blisko) -> "ENX" powinno trafic w "TNX"
check("ENX -> TNX (E/T blisko)", L._best_match("ENX", {"TNX"}, max_dist=1) == "TNX")

print("\n=== _best_match: morse-daleka pomylka juz NIE jest poprawiana (regresja-strazniK) ===")
# ONX: O jest morse-dalekie od T (koszt 2 > max_dist=1) -> nie powinno trafic
check("ONX -> None (O/T daleko, max_dist=1)", L._best_match("ONX", {"TNX"}, max_dist=1) is None)

print("\n=== correct(): dotychczasowe zachowanie bez regresji ===")
check("RST passthrough 599", L.correct("TNX 599 GL") == "TNX 599 GL")
check("cut numbers 5NN -> 5NN (juz sensowny raport)", L.correct("UR 5NN") == "UR 5NN")
check("segmentacja TKSFER -> TKS FER", L.correct("TKSFER") == "TKS FER")
check("imie po NAME — przyklad z naglowka pliku (KEITH -> KEHTHA)",
      L.correct("NAME KEHTHA") == "NAME KEITH")
check("nieznany smiec zostaje bez zmian", L.correct("XQZVWK") == "XQZVWK")

print("\n=== correct(): known_calls — bliska pomylka poprawiona, odlegla nie ===")
calls = {"SQ3MZM"}
# Q<->0 sa morse-bliskie? Q=--.- O=--- sa rozne, sprawdzmy inny scenariusz:
# pojedyncza litera bliska w SQ3MZM np. M<->N (--  vs -.) koszt 1
res_close = L.correct("SQ3MZN", known_calls=calls)
check("SQ3MZN -> SQ3MZM (N/M blisko, koszt 1)", res_close == "SQ3MZM")

print("\n=== correct(): rozszerzone slownictwo (Q-kody, prosign BT, nowe imiona) ===")
check("BT (prosign) przechodzi bez zmian", "BT" in L.correct("DE SQ3MZM BT TEST").split())
check("QSK (nowy Q-kod) rozpoznany", "QSK" in L.correct("QSK PSE").split())
check("PIOTRA -> PIOTR (nowe imie, bliska pomylka)", L.correct("NAME PIOTRA") == "NAME PIOTR")

print("\n=== _segment(): sklejony znany znak i sklejony raport RST ===")
check("DESQ3MZM -> DE SQ3MZM (znany znak dolaczony do slowa)",
      L.correct("DESQ3MZM", known_calls={"SQ3MZM"}) == "DE SQ3MZM")
check("bez known_calls token zostaje bez zmian (nie zgadujemy)",
      L.correct("DESQ3MZM") == "DESQ3MZM")
check("5NNTU -> 5NN TU (raport doklejony do slowa)", L.correct("5NNTU") == "5NN TU")

print("\n=== _segment(): CZESCIOWE pokrycie (zgloszenie z zycia) ===")
# Zgloszony przypadek: 'TNX FER QSO 73' zlepilo sie w jeden ciag z doklejonym
# smieciowym ogonem 'TFE' (koniec transmisji/szum). Dawniej WHOLE dopasowanie
# wymagalo pelnego pokrycia -> caly ciag zostawal nieczytelny bez spacji.
check("TNXFERQSO73TFE -> czesciowo odzyskane sensowne slowa",
      L.correct("TNXFERQSO73TFE") == "TNX FER QSO 73 TFE")
check("wiekszosc smiecia (ponizej 50% rozpoznane) NIE jest dzielona",
      L.correct("XQZVWKTU") == "XQZVWKTU")

print("\n=== '44' — pozdrowienie/pozegnanie POTA/SOTA, jak 73/88 ===")
check("44 przechodzi bez zmian jako osobny token", L.correct("TU 44") == "TU 44")
check("GM5NN44TU -> GM 5NN 44 TU (44 rozpoznane, nie 'wyspa')",
      L.correct("GM5NN44TU") == "GM 5NN 44 TU")

print("\n=== _reglue_split_call: znak rozbity falszywymi przerwami ===")
# Zgloszony przypadek: 'CQ CQ DE HB9TWX PSE K' dekoder rozbil znak na
# pojedyncze litery ('H B 9 T WX'). Bez known_calls dajemy tylko czesciowe
# sklejenie wg ksztaltu (nie zgadujemy, ze WX to koniec znaku a nie
# prawdziwe slowo); z known_calls (znak spotkany np. na clustrze/FT8/logu)
# sklejamy w calosci.
check("bez known_calls: czesciowe sklejenie wg ksztaltu, WX zostaje osobno",
      L.correct("H B 9 T WX") == "HB9T WX")
check("z known_calls: pelne sklejenie do znanego znaku",
      L.correct("H B 9 T WX", known_calls={"HB9TWX"}) == "HB9TWX")
check("prawdziwe slowa (CQ/DE/PSE/K) nie zostaja polkniete przez sklejanie",
      L.correct("CQ CQ DE H B 9 T WX PSE K", known_calls={"HB9TWX"})
      == "CQ CQ DE HB9TWX PSE K")
check("pojedyncze krotkie prawdziwe slowo obok cyfry nie sklejane bez sensu",
      L.correct("TU K") == "TU K")

print(f"\n{'='*50}")
if failed == 0:
    print(f"  WYNIK: {passed}/{passed} - WSZYSTKO OK")
else:
    print(f"  WYNIK: {passed}/{passed+failed} - {failed} BLEDOW")
print("="*50)
