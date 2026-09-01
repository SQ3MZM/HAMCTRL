# Installation guide / Instrukcja instalacji

**[English](#english) | [Polski](#polski)**

---

<a id="english"></a>
## English

Requires **Windows**, a physical connection to the radio (USB, for the
reference IC-7300), and a few one-time settings both in the installer
and on the radio itself. None of it is hard, but skipping a step is the
most common cause of "it doesn't work."

### 1. Install

1. Download `HAMCTRL-Setup.exe` from the [latest release](../../releases/latest).
2. Run it and follow the installer. It bundles everything needed
   (Python runtime, the Rust audio engine, Opus codec) — no separate
   Python install required.
3. Windows/your antivirus may flag an unsigned installer from a new
   publisher — this is expected for a small open-source project with no
   code-signing certificate, not a sign of a problem.

### 2. First run

1. Launch HAMCTRL. It starts a local server and opens your browser at
   `https://localhost:8001`.
2. **The browser will warn about the certificate** ("Your connection
   isn't private" / a self-signed cert warning) — this is expected, the
   app generates its own certificate on first run so it can serve HTTPS
   (required for the microphone/TX-audio feature to work in the
   browser). Click **Advanced → Proceed to localhost**.
3. Log in with the default account: `admin` / `Admin1234!`
4. You'll immediately be asked to **set a new admin password** — the
   default cannot stay active. Pick one and continue.

### 3. Connect the radio

1. Plug the radio into the PC over USB (or the appropriate serial
   adapter for older models).
2. **On the radio itself**, in the CI-V menu:
   - Set the **CI-V baud rate to 115200** if you want the waterfall/scope
     (lower baud rates work for control-only, but the scope stream
     specifically requires 115200).
   - Set **CI-V USB Port → Unlink from REMOTE** (on the IC-7300, this is
     what actually lets a PC application read the scope data — without
     it you'll get radio control but no waterfall).
3. In HAMCTRL's **KONFIGURACJA / CONFIG** tab, pick your radio model and
   COM port, then connect.
4. If the radio doesn't respond, double check: correct COM port, matching
   baud rate on both ends, and that the radio is actually powered on —
   the app will attempt a wake-up sequence automatically, but can't help
   with a wrong port/baud/cable.

**Other radio models — needs Hamlib.** HAMCTRL talks CI-V directly (no
extra software) only for the radios with a built-in profile: IC-7300,
IC-7610, IC-705, IC-9100, IC-7100, IC-9700. Any other radio (older Icom
models, Yaesu, Kenwood, ...) goes through
[Hamlib](https://hamlib.github.io/)'s `rigctld`, which is **not**
bundled in the installer — download and install it separately (the
Windows build from the Hamlib site), then either let HAMCTRL find it
automatically (it looks in the default install paths and on `PATH`) or
point `.env`'s `HAMLIB_PATH` at `rigctld.exe` directly if you installed
it somewhere else.

### 4. Audio (for SSB voice and to hear the radio)

- Pick your RX (speaker/headphones) and TX (microphone) devices under
  **PROFIL**. If your radio provides USB audio directly (like the
  IC-7300), you can usually select it directly rather than a physical
  sound card.
- If audio ends up on the wrong output device (e.g. built-in speakers
  instead of headphones you plugged in later), explicitly pick the
  headphones by name in that same dropdown rather than leaving it on
  "Default" — the browser doesn't always re-detect a device you plugged
  in after the page already loaded.

### 5. Using it from another device on your network

Other computers/phones on the same LAN can reach the app at
`https://<this-PC's-LAN-IP>:8001` — check the console window HAMCTRL
opened at startup, it prints the exact URL. You may need to allow the
app through Windows Firewall the first time (Windows will usually prompt
for this automatically).

### 6. Optional: access from outside your network, COM-port bridge for other software

Both of these are more advanced, one-time setup steps. See the in-app
help text on the **INTERNET** and **KONFIGURACJA** tabs for the full
picture — covering every option here would make this guide longer than
it needs to be for a first install. The short version:

- **Quick internet access, no extra software**: a Cloudflare Quick
  Tunnel (under **INTERNET**) — gives you a working `https://...
  trycloudflare.com` address in seconds, nothing to install.
- **Your own domain with a real (non-self-signed) certificate**: needs
  [win-acme](https://www.win-acme.com/) installed separately — HAMCTRL
  does **not** bundle it. Certbot discontinued Windows support in
  February 2024, so win-acme is the replacement client. It has no
  installer — download the portable zip and extract `wacs.exe` to
  `C:\win-acme\`. Generating/renewing the certificate needs
  Administrator rights — use the **"Wygeneruj certyfikat (jako admin)"
  ("Generate certificate (as admin)")** shortcut created in the Start
  menu; it prompts for elevation (UAC) itself, you don't need to
  right-click it. Renewal afterward is automatic (a scheduled task,
  enabled by default at install time).
- **Virtual-COM-port bridge** for running CW Skimmer / Ham Radio Deluxe
  / Logger32 alongside HAMCTRL: requires
  [com0com](https://com0com.sourceforge.net/) on the client machine.

### Troubleshooting

- **Browser keeps warning about the certificate on every visit**: normal
  for a self-signed cert — either click through it each time, or trust
  it manually in your OS/browser certificate store (optional, cosmetic
  only).
- **Radio connects but no waterfall**: almost always the CI-V baud
  rate/USB-port-unlink setting on the radio (step 3 above).
- **Can't reach it from another device**: check the Windows Firewall
  prompt wasn't dismissed/blocked, and that you're using the LAN IP
  printed at startup, not `localhost`.

---

<a id="polski"></a>
## Polski

Wymaga **Windows**, fizycznego połączenia z radiem (USB, dla
referencyjnego IC-7300) i kilku jednorazowych ustawień — zarówno w
instalatorze, jak i w samym radiu. Nic z tego nie jest trudne, ale
pominięcie kroku to najczęstsza przyczyna "nie działa".

### 1. Instalacja

1. Pobierz `HAMCTRL-Setup.exe` z [najnowszego wydania](../../releases/latest).
2. Uruchom i przejdź przez instalator. Zawiera wszystko co potrzebne
   (środowisko Python, silnik audio w Rust, kodek Opus) — osobna
   instalacja Pythona niepotrzebna.
3. Windows/antywirus może ostrzec przed niepodpisanym instalatorem od
   nowego wydawcy — to normalne dla małego projektu open source bez
   certyfikatu podpisu kodu, nie oznacza problemu.

### 2. Pierwsze uruchomienie

1. Uruchom HAMCTRL. Wystartuje lokalny serwer i otworzy przeglądarkę pod
   `https://localhost:8001`.
2. **Przeglądarka ostrzeże o certyfikacie** ("Połączenie nie jest
   prywatne" / ostrzeżenie o certyfikacie z podpisem własnym) — to
   oczekiwane, aplikacja generuje własny certyfikat przy pierwszym
   uruchomieniu, żeby móc działać po HTTPS (wymagane, żeby funkcja
   mikrofonu/audio TX działała w przeglądarce). Kliknij **Zaawansowane →
   Przejdź do localhost**.
3. Zaloguj się domyślnym kontem: `admin` / `Admin1234!`
4. Od razu zostaniesz poproszony o **ustawienie nowego hasła
   administratora** — domyślne nie może zostać aktywne. Wybierz nowe i
   kontynuuj.

### 3. Podłączenie radia

1. Podłącz radio do komputera przez USB (albo odpowiedni adapter
   szeregowy dla starszych modeli).
2. **W samym radiu**, w menu CI-V:
   - Ustaw **prędkość CI-V na 115200**, jeśli chcesz mieć
     wodospad/scope (niższe prędkości wystarczą do samego sterowania,
     ale strumień scope wymaga konkretnie 115200).
   - Ustaw **CI-V USB Port → Unlink from REMOTE** (w IC-7300 to właśnie
     to ustawienie pozwala aplikacji na PC czytać dane scope'a — bez
     tego będzie działać sterowanie radiem, ale nie wodospad).
3. W zakładce **KONFIGURACJA** w HAMCTRL wybierz model radia i port COM,
   po czym połącz.
4. Jeśli radio nie odpowiada, sprawdź: właściwy port COM, zgodną
   prędkość po obu stronach, i czy radio jest faktycznie włączone —
   aplikacja sama spróbuje sekwencji "budzenia" radia, ale nie pomoże
   przy złym porcie/prędkości/kablu.

**Inne modele radia — potrzebny Hamlib.** HAMCTRL rozmawia po CI-V
bezpośrednio (bez dodatkowego softu) tylko z radiami, dla których ma
gotowy profil: IC-7300, IC-7610, IC-705, IC-9100, IC-7100, IC-9700.
Każde inne radio (starsze modele Icom, Yaesu, Kenwood...) idzie przez
`rigctld` z [Hamlib](https://hamlib.github.io/), którego instalator
**nie zawiera** — pobierz i zainstaluj osobno (build dla Windows ze
strony Hamlib), a potem albo pozwól HAMCTRL znaleźć go samemu (szuka w
domyślnych ścieżkach instalacji i w `PATH`), albo wskaż `rigctld.exe`
wprost przez `HAMLIB_PATH` w pliku `.env`, jeśli zainstalowałeś go
gdzie indziej.

### 4. Audio (do SSB i żeby słyszeć radio)

- Wybierz urządzenie RX (głośnik/słuchawki) i TX (mikrofon) w zakładce
  **PROFIL**. Jeśli radio udostępnia audio bezpośrednio przez USB (jak
  IC-7300), zwykle można wybrać je wprost, bez fizycznej karty dźwiękowej.
- Jeśli dźwięk trafia na złe urządzenie wyjścia (np. wbudowane głośniki
  zamiast podłączonych później słuchawek), wybierz słuchawki jawnie po
  nazwie w tym samym polu, zamiast zostawiać "Domyślne" — przeglądarka
  nie zawsze wykrywa urządzenie podłączone już po załadowaniu strony.

### 5. Korzystanie z innego urządzenia w sieci

Inne komputery/telefony w tej samej sieci lokalnej mogą wejść na
aplikację pod adresem `https://<IP-tego-komputera-w-sieci>:8001` —
dokładny adres jest wypisany w oknie konsoli, które HAMCTRL otwiera przy
starcie. Przy pierwszym uruchomieniu może być potrzebne zezwolenie
aplikacji w Zaporze Windows (Windows zwykle sam o to zapyta).

### 6. Opcjonalnie: dostęp spoza sieci, mostek COM dla innego softu

Oba to bardziej zaawansowane, jednorazowe ustawienia. Pełny opis
zobacz w tekście pomocy w samej aplikacji, w zakładkach **INTERNET** i
**KONFIGURACJA** — opisanie tu każdej opcji wydłużyłoby ten poradnik
ponad potrzeby pierwszej instalacji. Wersja skrócona:

- **Szybki dostęp z internetu, bez dodatkowego softu**: Cloudflare
  Quick Tunnel (zakładka **INTERNET**) — działający adres
  `https://...trycloudflare.com` w kilka sekund, nic nie trzeba
  instalować.
- **Własna domena z prawdziwym certyfikatem** (nie self-signed):
  potrzebny osobno zainstalowany [win-acme](https://www.win-acme.com/) —
  HAMCTRL go **nie** dołącza. Certbot zakończył wsparcie dla Windows w
  lutym 2024, więc win-acme jest jego następcą. Bez instalatora —
  pobierz portable zip i rozpakuj `wacs.exe` do `C:\win-acme\`.
  Generowanie/odnawianie certyfikatu wymaga uprawnień administratora —
  użyj skrótu **"Wygeneruj certyfikat (jako
  admin)"** w menu Start; sam poprosi o podniesienie uprawnień (UAC),
  nie trzeba klikać prawym przyciskiem. Późniejsze odnawianie jest
  automatyczne (zadanie w Harmonogramie, włączone domyślnie przy
  instalacji).
- **Mostek wirtualnych portów COM** do uruchomienia CW Skimmer / Ham
  Radio Deluxe / Logger32 równolegle z HAMCTRL: wymaga
  [com0com](https://com0com.sourceforge.net/) na komputerze klienckim.

### Rozwiązywanie problemów

- **Przeglądarka wciąż ostrzega o certyfikacie przy każdej wizycie**:
  normalne dla certyfikatu z podpisem własnym — albo klikaj "dalej" za
  każdym razem, albo zaufaj mu ręcznie w magazynie certyfikatów
  systemu/przeglądarki (opcjonalne, tylko kosmetyczne).
- **Radio się łączy, ale nie ma wodospadu**: prawie zawsze to ustawienie
  prędkości CI-V / Unlink from REMOTE w samym radiu (krok 3 powyżej).
- **Nie da się połączyć z innego urządzenia**: sprawdź czy prośba Zapory
  Windows nie została odrzucona/zablokowana, i czy używasz adresu IP w
  sieci lokalnej wypisanego przy starcie, a nie `localhost`.
