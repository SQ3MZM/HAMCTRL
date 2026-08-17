"""
Generator pojedynczego "slupka" wodospadu (waterfall column) z krotkiego
kawalka audio RX. Lekka, szybka funkcja przeznaczona do czestego
strumieniowania do UI (np. co 0.5-1s) — NIE do synchronizacji FT8 (do tego
sluzy sync.compute_magnitude_spectrogram, ktora ma wyzsza rozdzielczosc
i jest kosztowniejsza obliczeniowo).
"""
import numpy as np

SAMPLE_RATE = 12000
F_MIN = 200
F_MAX = 3000
N_BINS = 200  # liczba slupkow czestotliwosci w jednej kolumnie wodospadu (rozdzielczosc ~14Hz/bin)

# Stan globalny do WYGLADZONEGO, STABILNEGO W CZASIE skalowania kolorow.
# KRYTYCZNE: liczenie db_min/db_max od nowa dla KAZDEJ kolumny (np. z jej
# wlasnych percentyli) sprawia, ze ten sam fizyczny poziom sygnalu dostaje
# inny kolor w kazdej kolejnej kolumnie czasowej — wyglada to jak losowy
# szum zamiast stabilnych, poziomych linii tonow. Zamiast tego trzymamy
# wolno aktualizowany (EMA) zakres, ktory zmienia sie dopiero na przestrzeni
# wielu sekund.
_ema_db_lo = None
_ema_db_hi = None
_EMA_ALPHA = 0.05  # im mniejsze, tym wolniej sie dostosowuje (stabilniejszy obraz)

# Wygladzanie CZASOWE samej kolumny (per-bin), oddzielne od wygladzania
# zakresu kolorow powyzej. KAZDA kolumna to niezalezne, krotkie okno FFT
# (0.3-0.8s audio) — bez wygladzania sasiednie kolumny czasowe maja duza
# wariancje (rozny fragment szumu za kazdym razem), co dawalo poszarpany,
# "schodkowy" wyglad zamiast plynnych pionowych smug jak w prawdziwym
# WSJT-X (ktory uzywa znacznie gestszego strumienia i/lub dluzszych okien
# analizy). EMA w domenie MOCY (przed konwersja do dB) jest matematycznie
# poprawniejsze niz usrednianie samych wartosci dB.
_ema_power_column = None
_SMOOTH_ALPHA = 0.35  # wieksze = mniej wygladzania (szybsza reakcja na zmiany)


def smooth_column_power(power_column):
    """
    Wygladza kolumne mocy (PRZED konwersja do dB) w czasie, EMA per-bin.
    Wywolywane raz na kazda nowa kolumne w petli backendu, miedzy
    compute_waterfall_column (ktore liczy SUROWA kolumne) a konwersja do dB.
    Resetuje sie automatycznie jesli rozmiar (n_bins) sie zmieni.
    """
    global _ema_power_column
    if _ema_power_column is None or len(_ema_power_column) != len(power_column):
        _ema_power_column = power_column.copy()
    else:
        _ema_power_column += _SMOOTH_ALPHA * (power_column - _ema_power_column)
    return _ema_power_column.copy()


def compute_waterfall_column(audio_chunk, n_bins=N_BINS, f_min=F_MIN, f_max=F_MAX,
                              sample_rate=SAMPLE_RATE, smooth=True):
    """
    audio_chunk: numpy float array (mono, sample_rate Hz), dowolnej dlugosci
        >= kilkadziesiat ms (typowo 0.3-0.8s kawalek z bufora RX).
    smooth: jesli True (domyslnie), stosuje wygladzanie czasowe EMA per-bin
        (patrz smooth_column_power) przed konwersja do dB — eliminuje
        poszarpany, "schodkowy" wyglad wynikajacy z duzej wariancji miedzy
        kolejnymi, niezaleznymi krotkimi oknami FFT.
    Zwraca: numpy array dlugosci n_bins, wartosci w dB (znormalizowane do
        sensownego zakresu wyswietlania, NIE do dekodowania FT8).
    """
    if len(audio_chunk) < 64:
        return np.zeros(n_bins, dtype=np.float32)

    window = np.hanning(len(audio_chunk))
    spec = np.fft.rfft(audio_chunk * window)
    power = np.abs(spec) ** 2
    freqs = np.fft.rfftfreq(len(audio_chunk), d=1.0 / sample_rate)

    # Zbinuj do n_bins rownomiernie rozlozonych binow miedzy f_min i f_max
    bin_edges = np.linspace(f_min, f_max, n_bins + 1)
    out = np.zeros(n_bins, dtype=np.float64)
    bin_idx = np.digitize(freqs, bin_edges) - 1
    for i in range(n_bins):
        mask = bin_idx == i
        if np.any(mask):
            out[i] = np.mean(power[mask])

    if smooth:
        out = smooth_column_power(out)

    db = 10 * np.log10(out + 1e-12)
    return db.astype(np.float32)


def quantize_for_transport(db_column, db_min=None, db_max=None, threshold=0.28, steepness=4.5, floor=42):
    """
    Kwantyzuje kolumne dB do listy int 0-255 (1 bajt/bin zamiast float32),
    zeby zmniejszyc rozmiar payloadu JSON wysylanego co ~0.8s przez WS.

    Jesli db_min/db_max nie podane, uzywa WYGLADZONEGO (EMA) globalnego
    zakresu, aktualizowanego powoli na podstawie percentyli kazdej nowej
    kolumny — bezwzgledny poziom dB zalezy mocno od sprzetu/wzmocnienia
    wejscia audio, wiec zakres trzeba dostosowac dynamicznie, ALE musi
    pozostawac STABILNY miedzy kolejnymi kolumnami (inaczej ten sam
    fizyczny sygnal dostaje inny kolor co kolumne, co wyglada jak losowy
    szum zamiast czystych, poziomych linii tonow FT8).

    threshold/steepness: krzywa kontrastu typu SIGMOID (S-curve), NIE prosta
    gamma. Pomiar na realnym nagraniu pokazal, ze ~75% wartosci (typowe tlo/
    slaby szum, nie sygnal) ladowalo juz w okolicy 0.2-0.3 znormalizowanego
    zakresu — przy prostej gammie (<1.0) ten przedzial byl rozjasniany razem
    z prawdziwymi slabymi sygnalami, przez co CALE pasmo wygladalo na
    "zajete" i nie dalo sie odroznic wolnego miejsca od aktywnej transmisji.
    Sigmoid wyraznie PRZYCIEMNIA wszystko ponizej threshold (cisza/szum
    zostaje czarna) i WZMACNIA wszystko powyzej (nawet slabe sygnaly staja
    sie wyrazne) — daje to ostre, czytelne rozgraniczenie zamiast jednolitego
    rozjasnienia. steepness kontroluje jak ostre jest przejscie.

    floor: minimalna wartosc (0-255) dla TLA — zmierzone bezposrednio z
    referencyjnego zrzutu ekranu JTDX, gdzie typowy szum renderuje sie jako
    nasycony niebieski (nie czarny). Bez tego, w bardzo cichych pasmach caly
    obraz robil sie czarny (matematycznie poprawne, ale mniej czytelne/znajome
    wizualnie niz oryginal). floor podnosi minimum tak, zeby "dywan" szumu byl
    zawsze widoczny, a kontrast wzgledem prawdziwych sygnalow byl zachowany
    (bo floor jest dodawany PO sigmoidzie, jako podloga, nie przesuniecie
    calej krzywej).
    """
    global _ema_db_lo, _ema_db_hi
    if db_min is None or db_max is None:
        # Uzywamy p10 (10. percentyl) jako dolnej granicy — szerszy zakres danych
        # jest rozlozony w palecie, dzieki czemu tlo szumu laduje w kolorowym
        # niebieskim (~40-80/255) zamiast prawie czarnym (floor=18/255 jak poprzednio).
        # To daje zywszy, bardziej czytelny waterfall podobny do WSJT-X/JTDX.
        # EMA wygladza zakres miedzy kolumnami, zeby kolory byly stabilne.
        p_lo = float(np.percentile(db_column, 10))  # p10 zamiast p50 — szerszy zakres danych w palecie
        p_hi = float(np.percentile(db_column, 99.5))
        if p_hi - p_lo < 10:
            p_hi = p_lo + 10
        target_lo = p_lo
        target_hi = p_hi + 3
        if _ema_db_lo is None:
            _ema_db_lo, _ema_db_hi = target_lo, target_hi
        else:
            _ema_db_lo += _EMA_ALPHA * (target_lo - _ema_db_lo)
            _ema_db_hi += _EMA_ALPHA * (target_hi - _ema_db_hi)
        db_min, db_max = _ema_db_lo, _ema_db_hi
    clipped = np.clip(db_column, db_min, db_max)
    norm = (clipped - db_min) / (db_max - db_min)  # 0..1

    # Sigmoid wysrodkowany na 'threshold', znormalizowany tak zeby norm=0 -> 0
    # i norm=1 -> 1 (zachowuje pelny zakres czerni/bieli na krancach).
    s = 1.0 / (1.0 + np.exp(-steepness * (norm - threshold)))
    s0 = 1.0 / (1.0 + np.exp(steepness * threshold))
    s1 = 1.0 / (1.0 + np.exp(-steepness * (1.0 - threshold)))
    contrast = (s - s0) / (s1 - s0)
    contrast = np.clip(contrast, 0.0, 1.0)

    scaled = contrast * 255
    # Podloga: minimalna widoczna wartosc, zeby tlo nigdy nie bylo calkowicie
    # czarne (jak w referencyjnym JTDX). Skalujemy reszte zakresu (floor..255)
    # zeby najsilniejsze sygnaly nadal osiagaly pelne 255.
    scaled = floor + scaled * (255 - floor) / 255.0
    scaled = np.clip(scaled, 0, 255).astype(np.uint8)
    return scaled.tolist()
