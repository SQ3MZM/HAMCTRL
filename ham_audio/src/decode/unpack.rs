/// Unpack 77-bit FT8 message into human-readable text.
/// Port of unpack.py — verified against WSJT-X ft8code.exe.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

// Constants from ft8_encoder.py
const NBASE_CQ: u32 = 2;
const STD_CALL_OFFSET: u32 = 6_257_896;
const NTOKENS: u32 = 2_063_592;

// Cache hashy znakow: DWUPOZIOMOWY. Standardowe pole c28 uzywa hasha
// 22-bitowego, ale wiadomosci typu i3=4 (znaki niestandardowe, np. F/SQ3MZM)
// adresuja odbiorce hashem 12-bitowym. Pamietamy kazdy widziany pelny znak
// pod OBOMA hashami, zeby <...> dalo sie odwrocic w obu kontekstach.
struct HashMaps {
    h22: HashMap<u32, String>,
    h12: HashMap<u32, String>,
    // h10: hash Foxa w wiadomosciach DXpedition (i3=0.1) - patrz unpack_type0_1.
    h10: HashMap<u32, String>,
}

static HASH_CACHE: OnceLock<Mutex<HashMaps>> = OnceLock::new();

fn hash_cache() -> std::sync::MutexGuard<'static, HashMaps> {
    HASH_CACHE.get_or_init(|| Mutex::new(HashMaps {
        h22: HashMap::new(),
        h12: HashMap::new(),
        h10: HashMap::new(),
    })).lock().unwrap()
}

fn ihashcall(call: &str, m: u32) -> u32 {
    let alphabet = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ/";
    let padded: String = format!("{:<11}", call.to_uppercase());
    let c0: &str = &padded[..11];
    let mut n8: u64 = 0;
    for ch in c0.chars() {
        let j = alphabet.find(ch).unwrap_or(0) as u64;
        n8 = 38u64.wrapping_mul(n8).wrapping_add(j);
    }
    let prod = 47_055_833_459u64.wrapping_mul(n8);
    (prod >> (64 - m)) as u32
}

pub fn remember_call(call: &str) {
    // Zapamietaj pelny znak pod hashem 22-bit (pole c28) i 12-bit (i3=4).
    // Znaki w <> to juz-hashe (nie pelne), _ to placeholder — pomijamy.
    let call = call.trim();
    if call.is_empty() || call.starts_with('<') || call.contains('_') {
        return;
    }
    let h22 = ihashcall(call, 22);
    let h12 = ihashcall(call, 12);
    let h10 = ihashcall(call, 10);
    let mut map = hash_cache();
    if map.h22.len() >= 2000 {
        if let Some(k) = map.h22.keys().next().copied() { map.h22.remove(&k); }
    }
    if map.h12.len() >= 2000 {
        if let Some(k) = map.h12.keys().next().copied() { map.h12.remove(&k); }
    }
    if map.h10.len() >= 2000 {
        if let Some(k) = map.h10.keys().next().copied() { map.h10.remove(&k); }
    }
    map.h22.insert(h22, call.to_string());
    map.h12.insert(h12, call.to_string());
    map.h10.insert(h10, call.to_string());
}

fn lookup_hash(h: u32) -> Option<String> {
    hash_cache().h22.get(&h).cloned()
}

fn lookup_hash12(h: u32) -> Option<String> {
    hash_cache().h12.get(&h).cloned()
}

fn lookup_hash10(h: u32) -> Option<String> {
    hash_cache().h10.get(&h).cloned()
}

fn bits_to_u32(bits: &[u8]) -> u32 {
    bits.iter().fold(0u32, |acc, &b| (acc << 1) | (b as u32 & 1))
}

fn unpack_c28(c28: u32) -> String {
    if c28 == 0 { return "DE".to_string(); }
    if c28 == 1 { return "QRZ".to_string(); }
    if c28 == NBASE_CQ { return "CQ".to_string(); }
    // Two distinct CQ-suffix ranges (packjt77.f90 unpack28), not one raw
    // number as previously coded here. Confirmed against real captured
    // traffic: c28 values that fall in the second range decode a real word
    // ("QRP", "POTA", "SOTA", ...), not a numeric id - the previous
    // single-range version printed that word's numeric encoding instead of
    // the word, e.g. "CQ_327404" instead of "CQ POTA".
    if c28 >= 3 && c28 <= 1002 {
        // 3-digit numeric CQ suffix (contest/exchange number), e.g. "CQ_042"
        return format!("CQ_{:03}", c28 - 3);
    }
    if c28 >= 1003 && c28 <= 532443 {
        // 4-character text modifier, base-27 (space + A-Z, no digits)
        const ALPHA27: [u8; 27] = *b" ABCDEFGHIJKLMNOPQRSTUVWXYZ";
        let mut n = c28 - 1003;
        let i1 = (n / (27 * 27 * 27)) as usize; n -= i1 as u32 * 27 * 27 * 27;
        let i2 = (n / (27 * 27)) as usize;      n -= i2 as u32 * 27 * 27;
        let i3 = (n / 27) as usize;             n -= i3 as u32 * 27;
        let i4 = n as usize;
        let modifier: String = [i1, i2, i3, i4].iter()
            .map(|&i| ALPHA27[i] as char).collect();
        return format!("CQ {}", modifier.trim());
    }
    if c28 >= NTOKENS && c28 < NTOKENS + 4_194_304 {
        let h = c28 - NTOKENS;
        if let Some(known) = lookup_hash(h) {
            return format!("<{}>", known);
        }
        return "<...>".to_string();
    }
    if c28 >= STD_CALL_OFFSET {
        let mut x = c28 - STD_CALL_OFFSET;
        let a5 = (x % 27) as usize; x /= 27;
        let a4 = (x % 27) as usize; x /= 27;
        let a3 = (x % 27) as usize; x /= 27;
        let a2 = (x % 10) as usize; x /= 10;
        let a1 = (x % 36) as usize; x /= 36;
        let a0 = x as usize;

        let letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

        let c0 = if a0 == 0 { ' ' }
                 else if a0 <= 10 { char::from_digit((a0 - 1) as u32, 10).unwrap() }
                 else { letters.chars().nth(a0 - 11).unwrap_or('?') };

        let c1 = if a1 <= 9 { char::from_digit(a1 as u32, 10).unwrap() }
                 else { letters.chars().nth(a1 - 10).unwrap_or('?') };

        let c2 = char::from_digit(a2 as u32, 10).unwrap();

        let suffix = |v: usize| -> char {
            if v == 0 { ' ' } else { letters.chars().nth(v - 1).unwrap_or('?') }
        };

        let call: String = format!("{}{}{}{}{}{}", c0, c1, c2, suffix(a3), suffix(a4), suffix(a5));
        // trim() (not trim_end()): a0==0 (1-letter prefix, e.g. "K1ABC") pads
        // c0 with a LEADING space too - trim_end() alone left it in, so any
        // single-letter-prefix call (extremely common in the US: K/N/W...)
        // decoded as " K1ABC". Invisible in HTML (collapses) and masked by
        // .trim() calls scattered through the JS comparison logic, which is
        // why this survived unnoticed - caught only by the new Hound type
        // 0.1 test, whose exact-equality assertion doesn't forgive it.
        return call.trim().to_string();
    }
    format!("<unk:{}>", c28)
}

// ── pack_c28: inverse of unpack_c28, for AP (a priori) decoding ────────
// AP needs to turn a KNOWN callsign hypothesis (e.g. the operator's own
// call) into the c28 bit pattern BP would have produced for it, to bias
// the channel LLR toward that hypothesis before retrying. Port of
// ft8_encoder.py's _encode_c28/_packcall (the authoritative, self-tested
// source - see that file's module docstring). v1 scope: standard 6-char
// callsigns only (covers "own call as TO", which is always a valid
// standard callsign) - the non-standard/hashed-call branch of
// _encode_c28 (_ihashcall fallback) is deferred; callers needing that can
// reuse ihashcall() above once AP grows past the own-call-as-TO case.

fn charn_inv_pos0(ch: char) -> Option<u32> {
    match ch {
        ' ' => Some(0),
        '0'..='9' => Some(ch as u32 - '0' as u32 + 1),
        'A'..='Z' => Some(ch as u32 - 'A' as u32 + 11),
        _ => None,
    }
}

fn charn_inv_pos1(ch: char) -> Option<u32> {
    match ch {
        '0'..='9' => Some(ch as u32 - '0' as u32),
        'A'..='Z' => Some(ch as u32 - 'A' as u32 + 10),
        _ => None,
    }
}

fn charn_inv_pos2(ch: char) -> Option<u32> {
    match ch {
        '0'..='9' => Some(ch as u32 - '0' as u32),
        _ => None,
    }
}

fn charn_inv_suffix(ch: char) -> Option<u32> {
    match ch {
        ' ' => Some(0),
        'A'..='Z' => Some(ch as u32 - 'A' as u32 + 1),
        _ => None,
    }
}

/// Port of ft8_encoder.py::_packcall. Returns the 21-bit standard-callsign
/// pack value (added to STD_CALL_OFFSET by the caller) or None if `call`
/// doesn't fit the standard 6-character field.
fn packcall(call: &str) -> Option<u32> {
    let mut call = call.trim().to_uppercase();
    // Historical protocol-mandated substitutions for two DXCC prefixes
    // whose call-area digit falls outside the two positions this packing
    // supports - matches the official spec exactly (one-way, not reversed
    // on decode either).
    if call.starts_with("3DA0") {
        call = format!("3D0{}", &call[4..]);
    } else if call.starts_with("3X") && call.len() > 2 && call.as_bytes()[2].is_ascii_alphabetic() {
        call = format!("Q{}", &call[2..]);
    }
    let bytes: Vec<char> = call.chars().collect();
    if bytes.len() >= 2 && bytes[1].is_ascii_digit()
        && !(bytes.len() >= 3 && bytes[2].is_ascii_digit()) {
        call = format!(" {}", call);
    }
    if call.len() > 6 { return None; }
    let padded: Vec<char> = format!("{:<6}", call).chars().collect();

    let a0 = charn_inv_pos0(padded[0])?;
    let a1 = charn_inv_pos1(padded[1])?;
    if !padded[2].is_ascii_digit() { return None; }
    let a2 = charn_inv_pos2(padded[2])?;
    let a3 = charn_inv_suffix(padded[3])?;
    let a4 = charn_inv_suffix(padded[4])?;
    let a5 = charn_inv_suffix(padded[5])?;

    let mut x = a0;
    x = x * 36 + a1;
    x = x * 10 + a2;
    x = x * 27 + a3;
    x = x * 27 + a4;
    x = x * 27 + a5;
    Some(x)
}

/// Strips a trailing /P or /R (portable/rover) suffix — only these two
/// map to FT8's separate r1 bit; any other suffix means a non-standard
/// callsign (out of scope for pack_c28's v1, see the module note above).
fn split_pr_suffix(call: &str) -> &str {
    let call = call.trim();
    if let Some(base) = call.strip_suffix("/P") { return base; }
    if let Some(base) = call.strip_suffix("/R") { return base; }
    call
}

/// Inverse of unpack_c28, for standard callsigns (+ CQ/DE/QRZ) only.
/// Returns None for anything that would need the hash-reference branch
/// (compound/prefixed calls, or one too long for the 6-character field) -
/// see the module note above.
pub fn pack_c28(call: &str) -> Option<u32> {
    let call = call.trim().to_uppercase();
    if call == "CQ"  { return Some(NBASE_CQ); }
    if call == "DE"  { return Some(0); }
    if call == "QRZ" { return Some(1); }
    let base = split_pr_suffix(&call);
    packcall(base).map(|v| STD_CALL_OFFSET + v)
}

fn ng_to_grid(ng: u32) -> String {
    let c3 = (ng % 10) as u8;
    let ng = ng / 10;
    let c2 = (ng % 10) as u8;
    let ng = ng / 10;
    let c1 = (ng % 18) as u8;
    let ng = ng / 18;
    let c0 = (ng % 18) as u8;
    format!("{}{}{}{}", (b'A' + c0) as char, (b'A' + c1) as char, c2, c3)
}

fn unpack_g15(g15: u32, r_flag: bool) -> (String, bool) {
    // Returns (report_or_grid, is_r_flag)
    //
    // ZWERYFIKOWANE wzgledem oryginalnego WSJT-X packjt77.f90 (subroutine
    // unpack77, sekcja "i3=1 .or. i3=2"). MAXGRID4 = 32400.
    //
    //   g15 <= 32400   -> Maidenhead grid (ng_to_grid)
    //   g15 == 32401   -> puste trzecie pole
    //   g15 == 32402   -> "RRR"      (ir musi byc 0)
    //   g15 == 32403   -> "RR73"     (ir musi byc 0)
    //   g15 == 32404   -> "73"       (ir musi byc 0)
    //   g15 >= 32405   -> raport, irpt = g15 - 32400, raport = irpt - 35
    //                     R-prefiks dodawany tylko gdy r_flag=true (osobny bit)
    if g15 <= 32400 { return (ng_to_grid(g15), r_flag); }
    if g15 == 32401 { return (String::new(), r_flag); }
    if g15 == 32402 { return ("RRR".to_string(), r_flag); }
    if g15 == 32403 { return ("RR73".to_string(), r_flag); }
    if g15 == 32404 { return ("73".to_string(), r_flag); }
    // g15 >= 32405 -> raport
    let irpt = (g15 - 32400) as i32;
    let report_val = irpt - 35; // zakres: g15=32405 -> -30, g15=32484 -> +49
    let prefix = if r_flag { "R" } else { "" };
    (format!("{}{:+03}", prefix, report_val), r_flag)
}

#[derive(Debug, Clone)]
pub struct Ft8Message {
    pub call_to:        String,
    pub call_de:        String,
    pub report_or_grid: String,
    /// Prefiks R raportu (R-15 vs -15). Wypelniany, konsument moze go czytac.
    #[allow(dead_code)]
    pub r_flag:         bool,
    pub message:        String,
    /// true dla wiadomosci typu 0.1 (i3=0, n3=1) - "FT8 DXpedition"/Fox
    /// compound message. W tym wypadku call_to/call_de NIE sa adresat/
    /// nadawca w zwyklym sensie - to dwa RUZNE znaki Houndow (jeden ktoremu
    /// Fox wysyla RR73, drugi ktorego zaprasza z raportem). Patrz
    /// unpack_type0_1 i FT8_DXpedition_Mode.pdf (K1JT) FAQ #7.
    pub is_dxpedition:   bool,
    /// Znak stacji ktora FAKTYCZNIE nadala te wiadomosc. Dla typow 1/2/4
    /// to zwyczajnie call_de (juz poprawny nadawca). Dla is_dxpedition=true
    /// call_to/call_de to dwaj Houndowie (NIE nadawca) - prawdziwy nadawca
    /// (Fox/stacja MSHV) jest tu osobno, rozwiazany z 10-bit hasha h10 (albo
    /// "..." jesli jeszcze nierozpoznany). Konsument (glowny automat w
    /// webapp.py, patrz qso_engine.parse_dxpedition_message) uzywa tego
    /// zamiast zgadywac nadawce z call_to/call_de.
    pub sender_call:     String,
    /// Rodzaj komunikatu: "std" (i3=1/2), "nonstd" (4), "dxped" (0.1),
    /// "text" (0.0), "fd" (0.3/0.4), "tel" (0.5), "rttyru" (3), "euvhf" (5).
    /// Automat QSO (webapp.py) i klik w oknie RX (wsjtx.js) reaguja tylko na
    /// std/nonstd/dxped - pozostale sa tylko wyswietlane, zeby np. wolny
    /// tekst "SQ3MZM DE SP3XYZ" nie zostal wziety za wywolanie od stacji "DE".
    pub msg_type:        &'static str,
}

fn bits_to_u64(bits: &[u8]) -> u64 {
    bits.iter().fold(0u64, |acc, &b| (acc << 1) | (b as u64 & 1))
}

pub fn unpack77(bits77: &[u8]) -> Option<Ft8Message> {
    debug_assert!(bits77.len() == 77);

    // Typ wiadomosci (i3, bity 74..77). Poprzednio IGNOROWANY — wszystko
    // dekodowane jak typ standardowy, przez co: (a) wiadomosci i3=4 (znaki
    // niestandardowe jak F/SQ3MZM) dawaly smieciowe dekody (nonsens-znaki
    // typu "624MOW"), (b) pelny znak niestandardowy nigdy nie trafial do
    // hash-cache, wiec wolajacy nas hashem zawsze pokazywal sie jako <...>
    // i automat nie wiedzial komu odpowiedziec.
    let i3 = bits_to_u32(&bits77[74..77]);

    match i3 {
        0 => {
            // Podtyp n3 (bity 71..74) - i3=0 to rodzina "kompaktowych"
            // wiadomosci (0.0 free text, 0.1 DXpedition/Fox, 0.3/0.4 Field
            // Day, 0.5 telemetria). Hound mode potrzebuje tylko 0.1 - patrz
            // unpack_type0_1.
            let n3 = bits_to_u32(&bits77[71..74]);
            match n3 {
                0     => unpack_free_text(bits77),
                1     => unpack_type0_1(bits77),
                3 | 4 => unpack_field_day(bits77, n3),
                5     => unpack_telemetry(bits77),
                // 0.2 nieuzywany, 0.6 WSPR, 0.7 zarezerwowany - odrzucamy
                // (bledna interpretacja pol dawalaby fantomy).
                _     => None,
            }
        }
        1 | 2 => unpack_standard(bits77, i3),
        3     => unpack_rtty_ru(bits77),
        4     => unpack_nonstandard(bits77),
        5     => unpack_eu_vhf(bits77),
        // i3=6/7 nie sa zdefiniowane w protokole.
        _     => None,
    }
}

fn unpack_standard(bits77: &[u8], i3: u32) -> Option<Ft8Message> {
    let c28_1 = bits_to_u32(&bits77[0..28]);
    let r1_1 = bits77[28] != 0;
    let c28_2 = bits_to_u32(&bits77[29..57]);
    let r1_2 = bits77[57] != 0;
    let r_flag = bits77[58] != 0;  // R1 bit
    let g15 = bits_to_u32(&bits77[59..74]);

    let mut call_to = unpack_c28(c28_1);
    let mut call_de = unpack_c28(c28_2);
    // Sufiksy z bitow r1: i3=1 -> "/R" (rover), i3=2 -> "/P" (portable,
    // EU VHF contest). WSJT-X pokazuje pelny znak z sufiksem.
    let suffix = if i3 == 2 { "/P" } else { "/R" };
    if r1_1 && !call_to.starts_with('<') && call_to != "CQ" {
        call_to.push_str(suffix);
    }
    if r1_2 && !call_de.starts_with('<') {
        call_de.push_str(suffix);
    }

    let (report_or_grid, r_flag) = unpack_g15(g15, r_flag);

    // Update hash cache with full callsigns seen
    for call in [&call_to, &call_de] {
        if !call.is_empty()
            && call != "CQ" && call != "DE" && call != "QRZ"
            && !call.starts_with('<') && !call.starts_with("CQ_")
        {
            remember_call(call);
        }
    }

    let message = format_message(&call_to, &call_de, &report_or_grid);
    let sender_call = call_de.clone(); // typ 1/2: call_de JUZ jest prawdziwym nadawca

    Some(Ft8Message {
        call_to,
        call_de,
        report_or_grid,
        r_flag,
        message,
        is_dxpedition: false,
        sender_call,
        msg_type: "std",
    })
}

/// Typ i3=0, n3=1: wiadomosc "FT8 DXpedition" (Fox/Hound). Fox zamyka jedno
/// QSO (RR73 dla call1) i JEDNOCZESNIE zaprasza kolejna stacje z raportem
/// (call2) - jedna transmisja, dwa Houndy. Uklad bitow (K9AN/G4WJS/K1JT,
/// "The FT4 and FT8 Communication Protocols", QEX Jul/Aug 2020, Table 1):
/// c28(call1) c28(call2) h10(hash wlasnego znaku Foxa) r5(raport dla call2).
/// To NAJCZESTSZA forma odpowiedzi Foxa w ruchliwym pile-upie (kazde
/// zamkniecie QSO przy niepustej kolejce laczy sie z zaproszeniem
/// nastepnego) - bez tej funkcji Hound regularnie nie widzial ani swojego
/// RR73, ani swojego zaproszenia z raportem (i3=0 bylo wczesniej calkiem
/// odrzucane w unpack77). Fox mode sam nie jest i nie bedzie implementowany
/// w tym projekcie (patrz CLAUDE.md) - to dekodowanie jest wylacznie po to,
/// zeby Hound (_hound w wsjtx.js) mogl poprawnie ROZUMIEC prawdziwego Foxa.
fn unpack_type0_1(bits77: &[u8]) -> Option<Ft8Message> {
    let c28_1 = bits_to_u32(&bits77[0..28]);
    let c28_2 = bits_to_u32(&bits77[28..56]);
    let h10   = bits_to_u32(&bits77[56..66]);
    let r5    = bits_to_u32(&bits77[66..71]);

    let call1 = unpack_c28(c28_1);
    let call2 = unpack_c28(c28_2);

    for call in [&call1, &call2] {
        if !call.is_empty() && !call.starts_with('<') && call != "CQ" {
            remember_call(call);
        }
    }

    // r5: 0-31 -> -30..+32 dB, co 2 (patrz Table 2 w QEX QEX Jul/Aug 2020).
    let report = (r5 as i32) * 2 - 30;
    let report_str = format!("{:+03}", report);
    let fox_disp = lookup_hash10(h10).unwrap_or_else(|| "...".to_string());
    let message = format!("{} RR73 {} <{}> {}", call1, call2, fox_disp, report_str);

    Some(Ft8Message {
        call_to:        call1,
        call_de:        call2,
        report_or_grid: report_str,
        r_flag:         false,
        message,
        is_dxpedition:  true,
        sender_call:    fox_disp, // NIE call1/call2 - prawdziwy nadawca (Fox/MSHV)
        msg_type:       "dxped",
    })
}

/// Typ i3=4: wiadomosc z NIESTANDARDOWYM znakiem (np. F/SQ3MZM, 3DA0RS/P).
/// Uklad pol (packjt77.f90): h12(12) | c58(58) | iflip(1) | nrpt(2) | icq(1)
///   h12   — 12-bitowy hash DRUGIEGO znaku (standardowego korespondenta)
///   c58   — pelny znak niestandardowy, 11 znakow base-38
///   iflip — 0: hash jest odbiorca ("<X> FULLCALL ..."), 1: odwrotnie
///   nrpt  — 0:"" 1:"RRR" 2:"RR73" 3:"73"
///   icq   — 1: to jest CQ ("CQ FULLCALL")
fn unpack_nonstandard(bits77: &[u8]) -> Option<Ft8Message> {
    const ALPHA38: &[u8] = b" 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ/";

    let h12   = bits_to_u32(&bits77[0..12]);
    let c58   = bits_to_u64(&bits77[12..70]);
    let iflip = bits77[70] != 0;
    let nrpt  = bits_to_u32(&bits77[71..73]);
    let icq   = bits77[73] != 0;

    // Dekoduj 11 znakow base-38 (MSB-first przy pakowaniu -> od tylu).
    let mut n = c58;
    let mut chars = [b' '; 11];
    for i in (0..11).rev() {
        chars[i] = ALPHA38[(n % 38) as usize];
        n /= 38;
    }
    let full_call = String::from_utf8_lossy(&chars).trim().to_string();
    if full_call.is_empty() {
        return None;
    }
    // KLUCZOWE: zapamietaj pelny znak niestandardowy — od tej chwili hash
    // tej stacji (w polach c28/h12 kolejnych wiadomosci) bedzie odwracalny
    // i zamiast <...> pokazemy <FULLCALL>.
    remember_call(&full_call);

    let rpt = match nrpt { 1 => "RRR", 2 => "RR73", 3 => "73", _ => "" };

    let (call_to, call_de) = if icq {
        ("CQ".to_string(), full_call.clone())
    } else {
        let hashed = match lookup_hash12(h12) {
            Some(known) => format!("<{}>", known),
            None => "<...>".to_string(),
        };
        if iflip {
            // pelny znak jest ODBIORCA, hash to nadawca
            (full_call.clone(), hashed)
        } else {
            // hash to odbiorca, pelny znak NADAJE (typowy przypadek:
            // "<SQ3MZM> F/DL1ABC RRR")
            (hashed, full_call.clone())
        }
    };

    let report_or_grid = rpt.to_string();
    let message = format_message(&call_to, &call_de, &report_or_grid);
    let sender_call = call_de.clone(); // typ 4: call_de JUZ jest prawdziwym nadawca

    Some(Ft8Message {
        call_to,
        call_de,
        report_or_grid,
        r_flag: false,
        message,
        is_dxpedition: false,
        sender_call,
        msg_type: "nonstd",
    })
}

fn bits_to_u128(bits: &[u8]) -> u128 {
    bits.iter().fold(0u128, |acc, &b| (acc << 1) | (b as u128 & 1))
}

/// Komunikat bez pary znakow (wolny tekst, telemetria) - call_to/call_de
/// puste, zeby cache grid/last-heard w webapp.py ich nie lapaly.
fn text_only(message: String, msg_type: &'static str) -> Option<Ft8Message> {
    if message.is_empty() { return None; }
    Some(Ft8Message {
        call_to: String::new(), call_de: String::new(), report_or_grid: String::new(),
        r_flag: false, message, is_dxpedition: false, sender_call: String::new(), msg_type,
    })
}

/// Komunikat zawodowy z dwoma znakami (Field Day, RTTY RU, EU VHF):
/// call_to/call_de jak w standardowym, reszta wymiany w report_or_grid.
fn contest_msg(call_to: String, call_de: String, exch: String, r_flag: bool,
               message: String, msg_type: &'static str) -> Option<Ft8Message> {
    for call in [&call_to, &call_de] {
        if !call.is_empty() && !call.starts_with('<') { remember_call(call); }
    }
    let sender_call = call_de.clone();
    Some(Ft8Message {
        call_to, call_de, report_or_grid: exch, r_flag, message,
        is_dxpedition: false, sender_call, msg_type,
    })
}

/// Typ 0.0: wolny tekst - 13 znakow base-42 z 71 bitow.
fn unpack_free_text(bits77: &[u8]) -> Option<Ft8Message> {
    const ALPHA42: &[u8] = b" 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-./?";
    let mut n = bits_to_u128(&bits77[0..71]);
    let mut chars = [b' '; 13];
    for i in (0..13).rev() {
        chars[i] = ALPHA42[(n % 42) as usize];
        n /= 42;
    }
    text_only(String::from_utf8_lossy(&chars).trim().to_string(), "text")
}

/// Typ 0.5: telemetria - 71 bitow jako 18 cyfr hex (23+24+24 bity),
/// bez zer wiodacych.
fn unpack_telemetry(bits77: &[u8]) -> Option<Ft8Message> {
    let a = bits_to_u32(&bits77[0..23]);
    let b = bits_to_u32(&bits77[23..47]);
    let c = bits_to_u32(&bits77[47..71]);
    let hex = format!("{:06X}{:06X}{:06X}", a, b, c);
    text_only(hex.trim_start_matches('0').to_string(), "tel")
}

const ARRL_SECTIONS: [&str; 86] = [
    "AB","AK","AL","AR","AZ","BC","CO","CT","DE","EB",
    "EMA","ENY","EPA","EWA","GA","GH","IA","ID","IL","IN",
    "KS","KY","LA","LAX","NS","MB","MDC","ME","MI","MN",
    "MO","MS","MT","NC","ND","NE","NFL","NH","NL","NLI",
    "NM","NNJ","NNY","TER","NTX","NV","OH","OK","ONE","ONN",
    "ONS","OR","ORG","PAC","PR","QC","RI","SB","SC","SCV",
    "SD","SDG","SF","SFL","SJV","SK","SNJ","STX","SV","TN",
    "UT","VA","VI","VT","WCF","WI","WMA","WNY","WPA","WTX",
    "WV","WWA","WY","DX","PE","NB",
];

/// Typ 0.3/0.4: ARRL Field Day - c28 c28 r1 n4(liczba TX-1) k3(klasa) s7(sekcja).
fn unpack_field_day(bits77: &[u8], n3: u32) -> Option<Ft8Message> {
    let n28a  = bits_to_u32(&bits77[0..28]);
    let n28b  = bits_to_u32(&bits77[28..56]);
    let ir    = bits77[56] != 0;
    let intx  = bits_to_u32(&bits77[57..61]);
    let class = bits_to_u32(&bits77[61..64]);
    let isec  = bits_to_u32(&bits77[64..71]) as usize;
    if isec < 1 || isec > ARRL_SECTIONS.len() || n28a <= 2 || n28b <= 2 { return None; }
    let call1 = unpack_c28(n28a);
    let call2 = unpack_c28(n28b);
    let ntx = intx + 1 + if n3 == 4 { 16 } else { 0 };
    let exch = format!("{}{} {}", ntx, (b'A' + class as u8) as char, ARRL_SECTIONS[isec - 1]);
    let message = format!("{} {} {}{}", call1, call2, if ir { "R " } else { "" }, exch);
    contest_msg(call1, call2, exch, ir, message, "fd")
}

// Stany USA + prowincje VE + kilka DX; indeksy 73..171 to "X01".."X99".
const RTTY_MULTS: [&str; 72] = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
    "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
    "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
    "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
    "NB","NS","QC","ON","MB","SK","AB","BC","NWT","NF",
    "LB","NU","YT","PEI","DC","DR","FR","GD","GR","OV",
    "ZH","ZL",
];

/// Typ 3: ARRL RTTY Roundup - t1(TU;) c28 c28 r1 r3(RST 5x9) s13(numer
/// seryjny albo 8000+indeks stanu/prowincji).
fn unpack_rtty_ru(bits77: &[u8]) -> Option<Ft8Message> {
    let itu   = bits77[0] != 0;
    let n28a  = bits_to_u32(&bits77[1..29]);
    let n28b  = bits_to_u32(&bits77[29..57]);
    let ir    = bits77[57] != 0;
    let irpt  = bits_to_u32(&bits77[58..61]);
    let nexch = bits_to_u32(&bits77[61..74]);
    let exch_tail = if nexch > 8000 {
        let imult = (nexch - 8000) as usize;
        if imult <= RTTY_MULTS.len() { RTTY_MULTS[imult - 1].to_string() }
        else if imult <= 171 { format!("X{:02}", imult - RTTY_MULTS.len()) }
        else { return None; }
    } else if nexch >= 1 && nexch <= 7999 {
        format!("{:04}", nexch)
    } else {
        return None;
    };
    let call1 = unpack_c28(n28a);
    let call2 = unpack_c28(n28b);
    let exch = format!("5{}9 {}", irpt + 2, exch_tail);
    let message = format!("{}{} {} {}{}", if itu { "TU; " } else { "" },
                          call1, call2, if ir { "R " } else { "" }, exch);
    contest_msg(call1, call2, exch, ir, message, "rttyru")
}

/// Typ 5: EU VHF contest - h12 h22 r1 r3(RS 52..59) s11(numer) g25(lokator 6-znakowy).
fn unpack_eu_vhf(bits77: &[u8]) -> Option<Ft8Message> {
    let n12     = bits_to_u32(&bits77[0..12]);
    let n22     = bits_to_u32(&bits77[12..34]);
    let ir      = bits77[34] != 0;
    let irpt    = bits_to_u32(&bits77[35..38]);
    let iserial = bits_to_u32(&bits77[38..49]);
    let mut n   = bits_to_u32(&bits77[49..74]);
    if n > 18_662_399 { return None; }
    let j1 = n / (18 * 10 * 10 * 24 * 24); n -= j1 * 18 * 10 * 10 * 24 * 24;
    let j2 = n / (10 * 10 * 24 * 24);      n -= j2 * 10 * 10 * 24 * 24;
    let j3 = n / (10 * 24 * 24);           n -= j3 * 10 * 24 * 24;
    let j4 = n / (24 * 24);                n -= j4 * 24 * 24;
    let j5 = n / 24;
    let j6 = n - j5 * 24;
    if j1 > 17 || j2 > 17 { return None; }
    let grid6: String = [b'A' + j1 as u8, b'A' + j2 as u8, b'0' + j3 as u8,
                         b'0' + j4 as u8, b'A' + j5 as u8, b'A' + j6 as u8]
        .iter().map(|&c| c as char).collect();
    let call1 = format!("<{}>", lookup_hash12(n12).unwrap_or_else(|| "...".to_string()));
    let call2 = format!("<{}>", lookup_hash(n22).unwrap_or_else(|| "...".to_string()));
    let exch = format!("{:02}{:04} {}", 52 + irpt, iserial, grid6);
    let message = format!("{} {} {}{}", call1, call2, if ir { "R " } else { "" }, exch);
    contest_msg(call1, call2, exch, ir, message, "euvhf")
}

#[cfg(test)]
mod tests {
    use super::{unpack_c28, unpack77, remember_call, pack_c28};

    #[test]
    fn pack_c28_roundtrips_through_unpack_c28() {
        for call in ["K1ABC", "W9XYZ", "G0XYZ", "SP3MZM", "SQ3MZM", "3Z1AC", "CQ", "DE", "QRZ"] {
            let packed = pack_c28(call).unwrap_or_else(|| panic!("pack_c28 failed for {call}"));
            assert_eq!(unpack_c28(packed), call, "round-trip mismatch for {call}");
        }
    }

    #[test]
    fn pack_c28_strips_portable_suffix() {
        // /P and /R map to FT8's separate r1 bit, not into c28 itself -
        // pack_c28 packs just the base call, same as unpack_c28 would
        // produce reading back a message that had /P set via that bit.
        let packed = pack_c28("SQ3MZM/P").expect("pack_c28 should strip /P and pack the base call");
        assert_eq!(unpack_c28(packed), "SQ3MZM");
    }

    #[test]
    fn pack_c28_rejects_compound_call() {
        // Compound calls (e.g. "F/SQ3MZM") don't fit the 6-char standard
        // field - v1 scope deliberately returns None here rather than
        // reaching for the hash-reference branch (see the module note on pack_c28).
        assert_eq!(pack_c28("F/SQ3MZM"), None);
    }

    // Regression test for a real bug found by comparing decode output
    // against a WSJT-X reference on a captured band recording: c28 values
    // in the alphanumeric CQ-modifier range (packjt77.f90 unpack28,
    // 1003..=532443) were being printed as a raw decimal number
    // ("CQ_327404") instead of decoded as the 4-character word it actually
    // represents ("CQ POTA"). Values below cross-checked by hand against
    // the base-27 (space+A-Z) encoding.
    #[test]
    fn cq_modifier_decodes_as_word_not_raw_number() {
        assert_eq!(unpack_c28(1003 + 12895), "CQ QRP");
        assert_eq!(unpack_c28(1003 + 326404), "CQ POTA");
        assert_eq!(unpack_c28(1003 + 385453), "CQ SOTA");
    }

    #[test]
    fn cq_numeric_suffix_still_decodes_as_number() {
        // 3-digit numeric CQ suffix range (contest/exchange number) is
        // unaffected by the modifier-range fix - still "CQ_NNN".
        assert_eq!(unpack_c28(3), "CQ_000");
        assert_eq!(unpack_c28(1002), "CQ_999");
    }

    #[test]
    fn cq_special_tokens_unaffected() {
        assert_eq!(unpack_c28(0), "DE");
        assert_eq!(unpack_c28(1), "QRZ");
        assert_eq!(unpack_c28(2), "CQ");
    }

    // Wektor bitowy dla przykladu z FT8_DXpedition_Mode.pdf (K1JT), strona 2:
    // "K1ABC RR73; W9XYZ <KH1/KH7Z> -08" - Fox=KH1/KH7Z zamyka QSO z K1ABC
    // (RR73) i jednoczesnie zaprasza W9XYZ z raportem -8dB. Wygenerowany
    // niezaleznie w Pythonie przy uzyciu JUZ zweryfikowanych _encode_c28/
    // _ihashcall z ft8_encoder.py (nie duplikujemy enkodera w Ruscie - Fox
    // mode nigdy nie bedzie nadawal tego typu, tylko odbierac).
    #[test]
    fn dxpedition_type0_1_decodes_combined_fox_message() {
        remember_call("KH1/KH7Z"); // symuluje ze widzielismy juz CQ Foxa
        let bits: [u8; 77] = [
            0,0,0,0,1,0,0,1,1,0,1,1,1,1,0,1,1,1,1,0,0,0,1,1,0,1,0,1,0,0,0,0,1,1,0,0,
            0,0,1,0,1,0,0,1,0,0,1,1,1,0,1,1,1,0,0,0,0,0,1,1,0,0,1,0,0,1,0,1,0,1,1,0,
            0,1,0,0,0,
        ];
        let msg = unpack77(&bits).expect("type 0.1 musi sie zdekodowac");
        assert!(msg.is_dxpedition);
        assert_eq!(msg.call_to, "K1ABC");       // dostaje RR73
        assert_eq!(msg.call_de, "W9XYZ");       // dostaje raport
        assert_eq!(msg.report_or_grid, "-08");
        assert!(msg.message.contains("KH1/KH7Z"), "hash Foxa powinien sie rozwiazac z cache: {}", msg.message);
        assert_eq!(msg.sender_call, "KH1/KH7Z"); // prawdziwy nadawca, NIE call_to/call_de
    }

    // Wektory 77-bitowe z ft8code.exe (WSJT-X) - po jednym na kazdy typ
    // komunikatu, ktory wczesniej byl odrzucany w unpack77.
    fn bits(s: &str) -> Vec<u8> {
        assert_eq!(s.len(), 77);
        s.bytes().map(|b| b - b'0').collect()
    }

    fn check(b: &str, text: &str, msg_type: &str) {
        let msg = unpack77(&bits(b)).unwrap_or_else(|| panic!("nie zdekodowano: {text}"));
        assert_eq!(msg.message, text);
        assert_eq!(msg.msg_type, msg_type);
    }

    #[test]
    fn free_text_type0_0() {
        check("00000000000000000000001000011010110010000111101011111000101011110000000000000", "TNX 73 GL", "text");
        let m = unpack77(&bits("01100000110101110101001011111111100111010011111110010001111101001001100000000")).unwrap();
        assert_eq!(m.message, "SQ3MZM DE SP3");
        // wolny tekst NIE moze udawac pary znakow (automat by odpowiedzial "DE")
        assert!(m.call_to.is_empty() && m.call_de.is_empty());
    }

    #[test]
    fn field_day_type0_3_and_0_4() {
        check("00001001101111011110001101010000110000101001001110111000001010001001100011000", "K1ABC W9XYZ 6A WI", "fd");
        check("00001100001010010011101110000000100110111101111000110101100000010001011100000", "W9XYZ K1ABC R 17B EMA", "fd");
    }

    #[test]
    fn telemetry_type0_5() {
        check("00100100011010001010110011110001001101010111100110111101111000000010010101000", "123456789ABCDEF012", "tel");
    }

    #[test]
    fn rtty_roundup_type3() {
        check("00000100110111101111000110101000011000010100100111011100001011111101110001011", "K1ABC W9XYZ 579 WI", "rttyru");
        check("10000110000101001001110111000000010011011110111100011010111100000000001101011", "TU; W9XYZ K1ABC R 589 0013", "rttyru");
        check("00000110000101001001110111000000010011011110111100011010100001111110010111011", "W9XYZ K1ABC 529 X15", "rttyru");
    }

    #[test]
    fn eu_vhf_type5() {
        remember_call("PA3XYZ");
        remember_call("G4ABC/P");
        // ft8code dla "<PA3XYZ> <G4ABC/P> ..." sam koduje w h12 hash
        // G4ABC/P i tak tez to wypisuje w kolumnie "Decoded" - zgodnosc z nim.
        check("11001000101111001000101111100100111111000000000110100010111010110000000111101", "<G4ABC/P> <G4ABC/P> R 590003 IO91NP", "euvhf");
    }
}

fn format_message(call_to: &str, call_de: &str, rg: &str) -> String {
    if rg.is_empty() {
        format!("{} {}", call_to, call_de)
    } else {
        format!("{} {} {}", call_to, call_de, rg)
    }
}
