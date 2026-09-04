; ============================================================================
;  HAMCTRL-installer.iss — instalator HAM RADIO CTRL (Inno Setup)
; ============================================================================
;  Buduje: HAMCTRL-Setup.exe
;
;  WYMAGA: Inno Setup 6+ (https://jrsoftware.org/isdl.php)
;
;  UZYCIE:
;    1. Zbuduj najpierw HAM-RADIO-CTRL.exe (py build_server.py)
;    2. Otworz ten plik w Inno Setup Compiler
;    3. Build -> Compile (albo F9)
;    4. Wynik: Output\HAMCTRL-Setup.exe
;
;  Instalator:
;    - instaluje HAM-RADIO-CTRL.exe do Program Files\HAM RADIO CTRL
;    - tworzy skroty (pulpit + menu Start)
;    - przy pierwszym uruchomieniu EXE otwiera przegladarke
;    - dane usera (config, users, .env) ida do %APPDATA%\HAMCTRL
;      (bo Program Files jest read-only - config.py to obsluguje)
;    - deinstalator w Panelu sterowania
; ============================================================================

#define AppName "HAM RADIO CTRL"
; Trzymaj zgodne z VERSION (root repo) i SERVER_VERSION w webapp.py -
; wszystkie trzy bumpowane razem tylko przy release, nie przy kazdym commicie
; (to inny numer niz znacznik [build] BUILD-YYYY-MM-DD-... w logu startowym,
; ktory zmienia sie prawie na kazdy commit i sluzy do potwierdzenia ze EXE
; spakowal najnowszy kod - nie do publicznego wersjonowania).
#define AppVersion "2.0.15"
#define AppPublisher "Franek (Claude.ai) & SQ3MZM Tom"
#define AppExeName "HAM-RADIO-CTRL.exe"
#define AppURL "https://github.com/SQ3MZM/HAMCTRL"

[Setup]
; AppId: unikalny identyfikator (nie zmieniaj miedzy wersjami tego samego produktu)
AppId={{A7F3C2E1-8B4D-4E6A-9C1F-HAMCTRL000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\HAM RADIO CTRL
DefaultGroupName=HAM RADIO CTRL
; Pozwol userowi wybrac katalog
DisableProgramGroupPage=yes
; Instalacja do Program Files wymaga admina
PrivilegesRequired=admin
OutputBaseFilename=HAMCTRL-Setup
OutputDir=Output
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Ikona instalatora (icon.ico obok skryptu budowania)
SetupIconFile=icon.ico
; Minimalne wersje Windows (Win10+)
MinVersion=10.0

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Auto-odnawianie certyfikatu Let's Encrypt co 60 dni (zalecane dla serwera
; dzialajacego 24/7 ze zdalnym dostepem). Domyslnie WLACZONE.
Name: "autocert"; Description: "Automatyczne odnawianie certyfikatu Let's Encrypt (co 60 dni, w tle)"; GroupDescription: "Zdalny dostep:"

[Files]
; Glowny EXE (spakowany serwer). Sciezka wzgledna - popraw jesli EXE gdzie indziej.
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Opcjonalnie README/instrukcja dla klubu
; Source: "README-KLUB.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
; Menu Start
Name: "{group}\HAM RADIO CTRL"; Filename: "{app}\{#AppExeName}"
; Reset hasla admina (gdy klub zapomni) - uruchamia EXE z flaga
Name: "{group}\Reset hasla admina"; Filename: "{app}\{#AppExeName}"; Parameters: "--reset-admin"
; Generowanie certyfikatu Let's Encrypt - win-acme wymaga admina. EXE sam
; prosi o podniesienie uprawnien (UAC) przy --gen-cert, wiec wystarczy
; kliknac skrot i potwierdzic monit. Osobny skrot uruchamiany raz na ~90 dni.
; Serwer na co dzien dziala BEZ admina.
Name: "{group}\Wygeneruj certyfikat (jako admin)"; Filename: "{app}\{#AppExeName}"; Parameters: "--gen-cert"
Name: "{group}\Odinstaluj HAM RADIO CTRL"; Filename: "{uninstallexe}"
; Pulpit (opcjonalny - task desktopicon)
Name: "{autodesktop}\HAM RADIO CTRL"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Regula firewall (raz przy instalacji) - pozwala na nasluch portow serwera,
; zeby Defender nie pytal o dostep do sieci przy KAZDYM uruchomieniu.
; Obejmuje porty: 8000/8001 (HTTP/HTTPS), 9400-9444 (audio/FT8), 2238 (WSJT-X).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""HAM RADIO CTRL"" dir=in action=allow program=""{app}\{#AppExeName}"" enable=yes profile=any"; Flags: runhidden waituntilterminated; StatusMsg: "Konfiguracja zapory sieciowej..."
; Osobna regula PORTOWA dla ham_audio.exe. Ten plik jest spakowany w bundlu
; PyInstaller i rozpakowywany przy KAZDYM starcie do losowej sciezki _MEIPASS,
; wiec regula "na program" nie zadziala (sciezka za kazdym razem inna) i zapora
; pytalaby w kolko. Regula na PORTY (9400 ctrl, 9401 WS audio, 9443 WSS audio)
; dziala niezaleznie od sciezki pliku. Port 9444 to polaczenie wychodzace do
; Pythona - nie wymaga reguly wejsciowej.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""HAM RADIO CTRL Audio"" dir=in action=allow protocol=TCP localport=9400,9401,9443 enable=yes profile=any"; Flags: runhidden waituntilterminated; StatusMsg: "Konfiguracja zapory (audio)..."
; Zadanie w Harmonogramie: automatyczne odnawianie certyfikatu Let's Encrypt
; co 60 dni (cert wazny 90 dni - odnawiamy z zapasem). Uruchamia --gen-cert
; z najwyzszymi uprawnieniami (win-acme wymaga admina), w tle. Serwer sam
; podchwyci nowy cert przez hot-reload (bez restartu). PELNA bezobslugowosc.
; Uruchamiane tylko jesli user wybral zadanie autocert (domyslnie tak).
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /TN ""HAMCTRL Cert Renewal"" /TR ""\""{app}\{#AppExeName}\"" --gen-cert"" /SC DAILY /MO 60 /RL HIGHEST /F /RU SYSTEM"; Flags: runhidden waituntilterminated; StatusMsg: "Konfiguracja auto-odnawiania certyfikatu..."; Tasks: autocert
; Auto-uruchomienie po instalacji USUNIETE celowo. Powodowalo blad 740
; (niezgodnosc kontekstu elevacji: instalator dziala jako admin, aplikacja
; jako asInvoker). Na roznych maszynach klubowych bylo zawodne. User uruchamia
; aplikacje ze skrotu w menu Start / na pulpicie - to dziala niezawodnie.

[UninstallRun]
; Przy deinstalacji usun zadanie odnawiania certu
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""HAMCTRL Cert Renewal"" /F"; Flags: runhidden; RunOnceId: "DelCertTask"

[UninstallRun]
; Przy deinstalacji usun regule firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""HAM RADIO CTRL"""; Flags: runhidden; RunOnceId: "DelFwRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""HAM RADIO CTRL Audio"""; Flags: runhidden; RunOnceId: "DelFwRuleAudio"

[UninstallDelete]
; Przy deinstalacji NIE usuwamy danych usera z APPDATA (konfiguracja, konta,
; logi QSO) - user moze chciec je zachowac. Jesli chcesz czyscic, odkomentuj:
; Type: filesandordirs; Name: "{userappdata}\HAMCTRL"

[Messages]
; Komunikat koncowy - kieruje usera do skrotu (auto-uruchomienie usuniete).
; FIX: this used to be ONE unscoped block, so it stayed in Polish even when
; the user picked "english" as the installer language - the [Languages]
; picker only translated Inno Setup's OWN stock wizard text, never this
; custom message. Split into [Messages.polish]/[Messages.english] sections
; so it actually follows the chosen language, same fix as the app's own
; startup language below.
[Messages.polish]
FinishedLabelNoIcons=Instalacja HAM RADIO CTRL zakonczona.%n%nUruchom aplikacje ze skrotu "HAM RADIO CTRL" w menu Start. Przy pierwszym uruchomieniu ustawisz haslo administratora, a nastepnie otworzy sie przegladarka z panelem.
FinishedLabel=Instalacja HAM RADIO CTRL zakonczona.%n%nUruchom aplikacje ze skrotu "HAM RADIO CTRL" w menu Start lub na pulpicie. Przy pierwszym uruchomieniu ustawisz haslo administratora, a nastepnie otworzy sie przegladarka z panelem.

[Messages.english]
FinishedLabelNoIcons=HAM RADIO CTRL installation complete.%n%nLaunch the app from the "HAM RADIO CTRL" shortcut in the Start menu. On first run you'll set the admin password, then a browser window with the panel will open.
FinishedLabel=HAM RADIO CTRL installation complete.%n%nLaunch the app from the "HAM RADIO CTRL" shortcut in the Start menu or on the desktop. On first run you'll set the admin password, then a browser window with the panel will open.

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

// FIX: the app's UI always started in Polish regardless of which language
// was picked in this installer's own [Languages] picker - the picker only
// ever controlled the INSTALLER's wizard text, nothing carried the choice
// through to the app itself. This writes a small marker file that
// data.py::get_cfg() reads ONCE, on the app's very first start (before
// config.json exists), to seed the server-wide default UI language - see
// the matching comment there and in index.html's inline bootstrap script.
procedure CurStepChanged(CurStep: TSetupStep);
var
  LangCode: String;
  MarkerDir: String;
  MarkerFile: String;
begin
  if CurStep = ssPostInstall then
  begin
    if ActiveLanguage() = 'polish' then
      LangCode := 'pl'
    else
      LangCode := 'en';
    MarkerDir := ExpandConstant('{userappdata}\HAMCTRL');
    ForceDirectories(MarkerDir);
    MarkerFile := MarkerDir + '\install_lang.txt';
    SaveStringToFile(MarkerFile, LangCode, False);
  end;
end;

// Przy DEINSTALACJI pytamy czy usunac dane uzytkownika (konta, konfiguracja,
// logi QSO w %APPDATA%\HAMCTRL). Domyslnie NIE - zeby reinstalacja/aktualizacja
// nie kasowala kont. User moze wybrac pelne czyszczenie.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\HAMCTRL');
    if DirExists(DataDir) then
    begin
      if MsgBox('Czy usunac takze dane uzytkownika?' + #13#10 + #13#10 +
                'Obejmuje: konta i hasla, konfiguracje radia, logi QSO.' + #13#10 +
                'Lokalizacja: ' + DataDir + #13#10 + #13#10 +
                'TAK = pelne czyszczenie (wszystko znika)' + #13#10 +
                'NIE = zachowaj dane (przydatne przy reinstalacji)',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
