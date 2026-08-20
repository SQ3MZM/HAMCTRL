# HAMCTRL

## Komunikacja / Communication

Odpowiadaj w języku, w którym pisze użytkownik — jeśli pisze po polsku,
odpowiadaj wyłącznie po polsku (cała wypowiedź, bez angielskich wtrąceń typu
„Fix:”/„commit” użytych zamiast polskiego odpowiednika); jeśli pisze w innym
języku, odpowiadaj w tym języku. Nazwy własne z kodu (funkcje, zmienne, typy
wiadomości WS jak `wsjtx_decode`, `is_tx`, nazwy plików) zawsze zostają w
oryginalnej formie — to cytowanie kodu, nie wtrącanie obcego języka.

*(This project's own instructions used to hardcode "always respond in
Polish" — wrong for a public repo where contributors may not read Polish
at all. Mirror whatever language the user writes in instead.)*

## Co to za projekt

Webapp do zdalnego sterowania radiostacją amatorską Icom IC-7300 przez CI-V:
strojenie, tryby, PTT, rotory, DX cluster, log QSO — oraz własny, kompletny
tor FT8/FT4 (dekoder + enkoder), **bez WSJT-X/JTDX**.

- Backend: **Python** (`aiohttp`), jeden duży plik `webapp.py` (~7000 linii) —
  serwer HTTP/WS, silnik automatyki FT8, sterowanie radiem (`civ.py`),
  audio (`audio.py`, `audio_stream.py`, `audio_rust_bridge.py`).
- Frontend: **vanilla JS** (bez frameworka/buildu) w `public/js/`, jeden
  `public/index.html`. Kluczowe dla FT8/FT4: `public/js/wsjtx.js` (okno
  RX/panel FT8) i `public/js/wsjtx_scope.js` (wodospad/scope).
- Audio real-time: osobny serwer w **Rust** w podfolderze `ham_audio/`
  (`ham_audio.exe`), komunikacja z Pythonem przez TCP (patrz
  `ft8_rust_receiver.py`). Rozróżnienie RX/TX jest istotne przy szukaniu
  błędów:
  - **RX (odbiór):** dekodowanie FT8/FT4 dzieje się w Rust
    (`ham_audio/src/decode/`), Python tylko odbiera gotowe wyniki przez
    TCP (`ft8_rust_receiver.py`).
  - **TX (nadawanie):** enkodowanie i generowanie sygnału FT8/FT4 jest po
    stronie Pythona — `ft8_encoder.py` i `ft4_encoder.py` tworzą PCM do
    nadawania. Cały tor TX (Fake Split, wybór częstotliwości, mnożenie
    poziomu `txVolume`, synchronizacja okna) jest w Pythonie. Szukając
    błędu w nadawaniu — zaczynać od Pythona, nie od Rusta.
- Produkcja działa jako **EXE spakowane PyInstallerem** (`hamctrl.spec`,
  punkt wejścia `launcher.py`) — **po każdej zmianie pliku .py trzeba
  przebudować EXE**, sama zmiana źródła nic nie daje na żywca.
- Sekrety i dane per-instalacja (`config.json`, `users.json`, certyfikaty
  TLS, `qso.db`) leżą w `%APPDATA%\HAMCTRL`, **nie w repo** — chroni je
  `.gitignore`, nie ruszać.

## Silnik FT8/FT4 (skąd zaczynać przy błędach automatyki/QSO)

Łańcuch: **Rust dekoduje → `ft8_rust_receiver.py` odbiera przez TCP →
`webapp.py::_ft8_rx_loop` → `_process_auto_qso` woła `qso_engine.py`
(czysta maszyna stanów QSO, bez asyncio/sieci) → broadcast WS
(`auto_qso_status`, `auto_seq_status`, `wsjtx_decode`, ...) → front
(`wsjtx.js`) renderuje okno RX i panel automatyki**.

- `qso_engine.py` — silnik stanu pojedynczego QSO + kolejka "Call 1st".
  Ma pełne pokrycie testami w `test_qso_engine.py` (uruchamiane jako
  skrypt, nie pytest: `python test_qso_engine.py`, wymaga
  `PYTHONIOENCODING=utf-8` w tej powłoce Windows, inaczej pada na
  polskich znakach w konsoli). **Każda naprawa w tym silniku powinna
  dostać swój test w tym pliku.**
- TX FT8/FT4 (`_ft8_tx_sequence` / `_ft8_tx_sequence_inner` w
  `webapp.py`) jest **wspólną ścieżką** dla nadawania ręcznego i
  automatycznego — synchronizacja do okna 15s/7.5s UTC, PTT, mutex
  `_ft8_tx_lock` (serializuje wszystkie transmisje, ręczne i auto).
- W kodzie są polskie komentarze opisujące wcześniejsze naprawy (np.
  „automatyka wariuje, backend i UI się rozjeżdżają") — czytać je,
  to ślady jak dany mechanizm miał działać i jakie regresje już raz
  naprawiono.
- Częsty wzorzec błędu w tym projekcie: front (`wsjtx.js`) wysyła/obsługuje
  typ wiadomości WS, dla którego backend **nigdy nie miał handlera**
  (albo handler zniknął) — objawia się jako „przycisk nic nie robi".
  Zawsze sprawdzać obie strony (`WS.send({type: ...})` we froncie vs
  `elif t == "..."` w `_ws_msg` w `webapp.py`).
- Świeżo połączony klient (nowa karta, F5, reconnect) musi dostać
  snapshot aktualnego stanu backendu zaraz po wiadomości `init` —
  bez tego panel automatyki/Fake Split pokazuje domyślne/nieaktualne
  wartości do czasu pierwszego kolejnego broadcastu.
- **Fake Split** (przesunięcie VFO na czas TX tak, by emisja trafiała w
  środek pasma odbiorczego korespondenta) był w torze TX i zniknął po
  serii zmian w torze audio — przywrócony 2026-08-10. Nazwy funkcji, po
  których szukać w historii Gita / śladach w kodzie, gdyby znów zniknął:
  `_compute_fake_split`, `_apply_fake_split_before_tx`,
  `_restore_fake_split_after_tx`, oraz handler WS
  `ft8_toggle_fake_split`. Domyślnie wyłączony, sterowany z panelu FT8 we
  froncie. VFO musi być zawsze przywrócony po TX (blok `finally`),
  inaczej radio zostaje na przesuniętej częstotliwości.

## Weryfikacja buildu i cache

- **Znacznik wersji EXE:** `webapp.py` wypisuje przy starcie linię
  `[build] webapp.py wersja BUILD-RRRR-MM-DD-...`. To narzędzie
  diagnostyczne: po przebudowie EXE sprawdzić w logu, czy znacznik jest
  aktualny. Jeśli widać stary znacznik mimo zmiany kodu — PyInstaller
  spakował niewłaściwy `webapp.py` (zły katalog/import), i żadna
  poprawka .py nie zadziała, dopóki to się nie naprawi. Przy istotnej
  zmianie warto podbić ten znacznik, żeby jednoznacznie potwierdzić, że
  nowy kod wszedł do EXE.
- **Cache przeglądarki (frontend):** pliki JS/CSS ładują się z `?v=...`
  (cache-busting) w `index.html`. Po zmianie w plikach `public/js/*.js`
  lub `public/css/style.css` podbić ten parametr wersji — inaczej
  przeglądarka poda stary plik z cache i test pokaże nieaktualny kod
  (objaw: „zmiana nie działa", choć plik na dysku jest poprawny).
  Alternatywnie twardy refresh (Ctrl+Shift+R) albo okno incognito do
  sprawdzenia. To częste źródło fałszywego „dalej to samo".

## Warsztat pracy

- Naprawiać **jedną rzecz naraz**. Po każdej zmianie w `qso_engine.py`
  lub w logice automatyki w `webapp.py`: uruchomić
  `PYTHONIOENCODING=utf-8 python test_qso_engine.py` i commitować
  dopiero gdy testy przechodzą.
- Nie dodawać abstrakcji/refaktoringu wykraczającego poza zgłoszony
  problem — ten plik jest już duży i gęsty, punktowe poprawki są tu
  cenniejsze niż porządkowanie.

## Kontekst użytkownika

Rozmówca jest operatorem radioamatorskim (znak SQ3MZM), nie programistą
etatowym — testuje zmiany na żywym sprzęcie (IC-7300 podłączony do
anteny). Konsekwencje:

- Instrukcje dawać konkretnie, krok po kroku (jaki plik, jaka komenda,
  co sprawdzić w logu).
- Szczególna ostrożność przy zmianach dotykających nadawania — sygnał
  idzie w eter. Przy strojeniu poziomów/mocy zaczynać od małej mocy.
- Objawy zgłaszane są w kategoriach działania radia („automat nie widzi
  wołających stacji", „ALC za wysokie"), nie w kategoriach kodu — trzeba
  je przełożyć na miejsce w kodzie.
