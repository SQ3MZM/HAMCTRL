/// CRC-14 polynomial 0x2757 — identical to WSJT-X implementation.

const POLY: u16 = 0x2757;

/// Compute CRC-14 over `bits` (0/1 values, 77 data bits + 5 zeros padding = 82 total).
pub fn crc14(bits: &[u8]) -> u16 {
    let mut crc: u16 = 0;
    for &b in bits {
        let msb = (crc >> 13) & 1;
        crc = (crc << 1) | (b as u16 & 1);
        if msb != 0 {
            crc ^= POLY;
        }
    }
    // Flush remaining CRC bits
    for _ in 0..14 {
        let msb = (crc >> 13) & 1;
        crc <<= 1;
        if msb != 0 {
            crc ^= POLY;
        }
    }
    crc & 0x3FFF
}

/// Extract 14-bit CRC from bits[77..91] and verify against bits[0..77].
pub fn check_crc(bits91: &[u8]) -> bool {
    debug_assert!(bits91.len() == 91);
    let mut padded = [0u8; 82];
    padded[..77].copy_from_slice(&bits91[..77]);
    // bits91[77..91] = received CRC
    let received: u16 = bits_to_u16(&bits91[77..91]);
    let computed = crc14(&padded);
    received == computed
}

fn bits_to_u16(bits: &[u8]) -> u16 {
    let mut v: u16 = 0;
    for &b in bits {
        v = (v << 1) | (b as u16 & 1);
    }
    v
}
