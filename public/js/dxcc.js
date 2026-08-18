/*
 * dxcc.js — Maps callsign prefixes to DXCC entities (countries).
 *
 * Simplified version of the full CTY.dat table — covers ~150 of the most
 * common countries seen on FT8/FT4. For full precision you could
 * download cty.dat from AA6YQ and parse it; this list is enough for most
 * operational purposes.
 *
 * Matching rules (longest to shortest - priority):
 *   1. Check 3-char prefixes first (VP2, VP6, VK9 - these are special)
 *   2. Then 2-char (SP, DL, F...)
 *   3. Finally 1-char (K, N, W - USA, G - England, etc.)
 *
 * We return an object: { name: "Poland", prefix: "SP", flag: "🇵🇱", continent: "EU" }
 * Empty if not found.
 */

(function() {
'use strict';

// Simplified DXCC table — the most common countries on FT8/FT4.
// Key = the longest matching prefix, value = {name, flag, continent}
// NOTE: order in the Map matters — special ones first (VP2, VK9), then
// 2-char ones. We use Object.entries + sort by key length.
const DXCC_TABLE = {
  // Poland
  'SP': {n:'Poland', f:'🇵🇱', c:'EU'},
  'SN': {n:'Poland', f:'🇵🇱', c:'EU'},
  'SO': {n:'Poland', f:'🇵🇱', c:'EU'},
  'SQ': {n:'Poland', f:'🇵🇱', c:'EU'},
  '3Z': {n:'Poland', f:'🇵🇱', c:'EU'},
  'HF': {n:'Poland', f:'🇵🇱', c:'EU'},

  // Germany
  'DL': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DA': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DB': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DC': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DD': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DE': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DF': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DG': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DH': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DJ': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DK': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DM': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DN': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DO': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DP': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DQ': {n:'Germany', f:'🇩🇪', c:'EU'},
  'DR': {n:'Germany', f:'🇩🇪', c:'EU'},

  // United Kingdom
  'G':  {n:'England', f:'🏴󠁧󠁢󠁥󠁮󠁧󠁿', c:'EU'},
  '2E': {n:'England', f:'🏴󠁧󠁢󠁥󠁮󠁧󠁿', c:'EU'},
  'M':  {n:'England', f:'🏴󠁧󠁢󠁥󠁮󠁧󠁿', c:'EU'},
  'GM': {n:'Scotland', f:'🏴󠁧󠁢󠁳󠁣󠁴󠁿', c:'EU'},
  'MM': {n:'Scotland', f:'🏴󠁧󠁢󠁳󠁣󠁴󠁿', c:'EU'},
  'GW': {n:'Wales', f:'🏴󠁧󠁢󠁷󠁬󠁳󠁿', c:'EU'},
  'MW': {n:'Wales', f:'🏴󠁧󠁢󠁷󠁬󠁳󠁿', c:'EU'},
  'GI': {n:'N. Ireland', f:'🇬🇧', c:'EU'},
  'MI': {n:'N. Ireland', f:'🇬🇧', c:'EU'},
  'GD': {n:'Isle of Man', f:'🇮🇲', c:'EU'},
  'MD': {n:'Isle of Man', f:'🇮🇲', c:'EU'},
  'GJ': {n:'Jersey', f:'🇯🇪', c:'EU'},
  'MJ': {n:'Jersey', f:'🇯🇪', c:'EU'},
  'GU': {n:'Guernsey', f:'🇬🇬', c:'EU'},
  'MU': {n:'Guernsey', f:'🇬🇬', c:'EU'},

  // France
  'F':  {n:'France', f:'🇫🇷', c:'EU'},
  'TM': {n:'France', f:'🇫🇷', c:'EU'},
  'FG': {n:'Guadeloupe', f:'🇬🇵', c:'NA'},
  'FM': {n:'Martinique', f:'🇲🇶', c:'NA'},
  'FY': {n:'French Guiana', f:'🇬🇫', c:'SA'},
  'FR': {n:'Reunion', f:'🇷🇪', c:'AF'},

  // Italy
  'I':  {n:'Italy', f:'🇮🇹', c:'EU'},
  'IK': {n:'Italy', f:'🇮🇹', c:'EU'},
  'IZ': {n:'Italy', f:'🇮🇹', c:'EU'},
  'IU': {n:'Italy', f:'🇮🇹', c:'EU'},
  'IW': {n:'Italy', f:'🇮🇹', c:'EU'},
  'IS': {n:'Sardinia', f:'🇮🇹', c:'EU'},
  'IH9': {n:'Pantelleria', f:'🇮🇹', c:'AF'},
  'IG9': {n:'Lampedusa', f:'🇮🇹', c:'AF'},

  // Spain
  'EA': {n:'Spain', f:'🇪🇸', c:'EU'},
  'EB': {n:'Spain', f:'🇪🇸', c:'EU'},
  'EC': {n:'Spain', f:'🇪🇸', c:'EU'},
  'ED': {n:'Spain', f:'🇪🇸', c:'EU'},
  'EE': {n:'Spain', f:'🇪🇸', c:'EU'},
  'EF': {n:'Spain', f:'🇪🇸', c:'EU'},
  'EA6': {n:'Balearic Is.', f:'🇪🇸', c:'EU'},
  'EA8': {n:'Canary Is.', f:'🇮🇨', c:'AF'},
  'EA9': {n:'Ceuta & Melilla', f:'🇪🇸', c:'AF'},

  // Russia and CIS
  'R':  {n:'Russia', f:'🇷🇺', c:'EU'},
  'RA': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RK': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RM': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RN': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RU': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RV': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RW': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RX': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RY': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RZ': {n:'Russia', f:'🇷🇺', c:'EU'},
  'UA': {n:'Russia', f:'🇷🇺', c:'EU'},
  'UB': {n:'Russia', f:'🇷🇺', c:'EU'},
  'UC': {n:'Russia', f:'🇷🇺', c:'EU'},
  'UD': {n:'Russia', f:'🇷🇺', c:'EU'},
  'UE': {n:'Russia', f:'🇷🇺', c:'EU'},
  'UI': {n:'Russia', f:'🇷🇺', c:'EU'},
  'RA0': {n:'Russia (As)', f:'🇷🇺', c:'AS'},
  'RA9': {n:'Russia (As)', f:'🇷🇺', c:'AS'},
  'UA0': {n:'Russia (As)', f:'🇷🇺', c:'AS'},
  'UA9': {n:'Russia (As)', f:'🇷🇺', c:'AS'},

  // Ukraine
  'UR': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'US': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'UT': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'UV': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'UW': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'UX': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'UY': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'UZ': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'EM': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'EN': {n:'Ukraine', f:'🇺🇦', c:'EU'},
  'EO': {n:'Ukraine', f:'🇺🇦', c:'EU'},

  // Belarus
  'EU': {n:'Belarus', f:'🇧🇾', c:'EU'},
  'EV': {n:'Belarus', f:'🇧🇾', c:'EU'},
  'EW': {n:'Belarus', f:'🇧🇾', c:'EU'},

  // Czechia, Slovakia
  'OK': {n:'Czech Rep.', f:'🇨🇿', c:'EU'},
  'OL': {n:'Czech Rep.', f:'🇨🇿', c:'EU'},
  'OM': {n:'Slovakia', f:'🇸🇰', c:'EU'},

  // Hungary
  'HA': {n:'Hungary', f:'🇭🇺', c:'EU'},
  'HG': {n:'Hungary', f:'🇭🇺', c:'EU'},

  // Romania, Bulgaria
  'YO': {n:'Romania', f:'🇷🇴', c:'EU'},
  'YP': {n:'Romania', f:'🇷🇴', c:'EU'},
  'YQ': {n:'Romania', f:'🇷🇴', c:'EU'},
  'YR': {n:'Romania', f:'🇷🇴', c:'EU'},
  'LZ': {n:'Bulgaria', f:'🇧🇬', c:'EU'},

  // Balkans
  'S5': {n:'Slovenia', f:'🇸🇮', c:'EU'},
  '9A': {n:'Croatia', f:'🇭🇷', c:'EU'},
  'E7': {n:'Bosnia', f:'🇧🇦', c:'EU'},
  'YU': {n:'Serbia', f:'🇷🇸', c:'EU'},
  'YT': {n:'Serbia', f:'🇷🇸', c:'EU'},
  'Z3': {n:'N. Macedonia', f:'🇲🇰', c:'EU'},
  'Z6': {n:'Kosovo', f:'🇽🇰', c:'EU'},
  '4O': {n:'Montenegro', f:'🇲🇪', c:'EU'},
  'ZA': {n:'Albania', f:'🇦🇱', c:'EU'},

  // Greece, Turkey, Cyprus
  'SV': {n:'Greece', f:'🇬🇷', c:'EU'},
  'J4': {n:'Greece', f:'🇬🇷', c:'EU'},
  'SW': {n:'Greece', f:'🇬🇷', c:'EU'},
  'SY': {n:'Greece', f:'🇬🇷', c:'EU'},
  'TA': {n:'Turkey', f:'🇹🇷', c:'EU'},
  'TB': {n:'Turkey', f:'🇹🇷', c:'EU'},
  'TC': {n:'Turkey', f:'🇹🇷', c:'EU'},
  '5B': {n:'Cyprus', f:'🇨🇾', c:'AS'},
  'C4': {n:'Cyprus', f:'🇨🇾', c:'AS'},
  'H2': {n:'Cyprus', f:'🇨🇾', c:'AS'},
  'P3': {n:'Cyprus', f:'🇨🇾', c:'AS'},

  // Scandinavia
  'SM': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'SA': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'SB': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'SI': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'SJ': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'SK': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'SL': {n:'Sweden', f:'🇸🇪', c:'EU'},
  'LA': {n:'Norway', f:'🇳🇴', c:'EU'},
  'LB': {n:'Norway', f:'🇳🇴', c:'EU'},
  'LI': {n:'Norway', f:'🇳🇴', c:'EU'},
  'LJ': {n:'Norway', f:'🇳🇴', c:'EU'},
  'LN': {n:'Norway', f:'🇳🇴', c:'EU'},
  'JW': {n:'Svalbard', f:'🇳🇴', c:'EU'},
  'JX': {n:'Jan Mayen', f:'🇳🇴', c:'EU'},
  'OH': {n:'Finland', f:'🇫🇮', c:'EU'},
  'OF': {n:'Finland', f:'🇫🇮', c:'EU'},
  'OG': {n:'Finland', f:'🇫🇮', c:'EU'},
  'OI': {n:'Finland', f:'🇫🇮', c:'EU'},
  'OJ': {n:'Finland', f:'🇫🇮', c:'EU'},
  'OZ': {n:'Denmark', f:'🇩🇰', c:'EU'},
  'OU': {n:'Denmark', f:'🇩🇰', c:'EU'},
  'OV': {n:'Denmark', f:'🇩🇰', c:'EU'},
  'OW': {n:'Denmark', f:'🇩🇰', c:'EU'},
  'OX': {n:'Greenland', f:'🇬🇱', c:'NA'},
  'OY': {n:'Faroe Is.', f:'🇫🇴', c:'EU'},
  'TF': {n:'Iceland', f:'🇮🇸', c:'EU'},

  // Benelux
  'ON': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'OO': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'OP': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'OQ': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'OR': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'OS': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'OT': {n:'Belgium', f:'🇧🇪', c:'EU'},
  'PA': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PB': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PC': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PD': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PE': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PF': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PG': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PH': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'PI': {n:'Netherlands', f:'🇳🇱', c:'EU'},
  'LX': {n:'Luxembourg', f:'🇱🇺', c:'EU'},

  // Alps
  'HB': {n:'Switzerland', f:'🇨🇭', c:'EU'},
  'HB0': {n:'Liechtenstein', f:'🇱🇮', c:'EU'},
  'HB9': {n:'Switzerland', f:'🇨🇭', c:'EU'},
  'OE': {n:'Austria', f:'🇦🇹', c:'EU'},

  // Baltics
  'ES': {n:'Estonia', f:'🇪🇪', c:'EU'},
  'YL': {n:'Latvia', f:'🇱🇻', c:'EU'},
  'LY': {n:'Lithuania', f:'🇱🇹', c:'EU'},

  // Portugal, Ireland
  'CT': {n:'Portugal', f:'🇵🇹', c:'EU'},
  'CQ': {n:'Portugal', f:'🇵🇹', c:'EU'},
  'CR': {n:'Portugal', f:'🇵🇹', c:'EU'},
  'CS': {n:'Portugal', f:'🇵🇹', c:'EU'},
  'CT3': {n:'Madeira', f:'🇵🇹', c:'AF'},
  'CU': {n:'Azores', f:'🇵🇹', c:'EU'},
  'EI': {n:'Ireland', f:'🇮🇪', c:'EU'},
  'EJ': {n:'Ireland', f:'🇮🇪', c:'EU'},

  // Malta, San Marino, Monaco
  '9H': {n:'Malta', f:'🇲🇹', c:'EU'},
  'T7': {n:'San Marino', f:'🇸🇲', c:'EU'},
  '3A': {n:'Monaco', f:'🇲🇨', c:'EU'},

  // North America
  'K':  {n:'USA', f:'🇺🇸', c:'NA'},
  'W':  {n:'USA', f:'🇺🇸', c:'NA'},
  'N':  {n:'USA', f:'🇺🇸', c:'NA'},
  'AA': {n:'USA', f:'🇺🇸', c:'NA'},
  'AB': {n:'USA', f:'🇺🇸', c:'NA'},
  'AC': {n:'USA', f:'🇺🇸', c:'NA'},
  'AD': {n:'USA', f:'🇺🇸', c:'NA'},
  'AE': {n:'USA', f:'🇺🇸', c:'NA'},
  'AF': {n:'USA', f:'🇺🇸', c:'NA'},
  'AG': {n:'USA', f:'🇺🇸', c:'NA'},
  'AI': {n:'USA', f:'🇺🇸', c:'NA'},
  'AJ': {n:'USA', f:'🇺🇸', c:'NA'},
  'AK': {n:'USA', f:'🇺🇸', c:'NA'},
  'KH6': {n:'Hawaii', f:'🇺🇸', c:'OC'},
  'KL': {n:'Alaska', f:'🇺🇸', c:'NA'},
  'KP4': {n:'Puerto Rico', f:'🇵🇷', c:'NA'},
  'NP4': {n:'Puerto Rico', f:'🇵🇷', c:'NA'},

  'VE': {n:'Canada', f:'🇨🇦', c:'NA'},
  'VA': {n:'Canada', f:'🇨🇦', c:'NA'},
  'VO': {n:'Canada', f:'🇨🇦', c:'NA'},
  'VY': {n:'Canada', f:'🇨🇦', c:'NA'},

  'XE': {n:'Mexico', f:'🇲🇽', c:'NA'},
  '6D': {n:'Mexico', f:'🇲🇽', c:'NA'},

  // Caribbean
  'CO': {n:'Cuba', f:'🇨🇺', c:'NA'},
  'CL': {n:'Cuba', f:'🇨🇺', c:'NA'},
  'CM': {n:'Cuba', f:'🇨🇺', c:'NA'},
  'HI': {n:'Dominican Rep.', f:'🇩🇴', c:'NA'},
  'HH': {n:'Haiti', f:'🇭🇹', c:'NA'},
  '4L': {n:'Georgia', f:'🇬🇪', c:'AS'},

  // South America
  'PY': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PP': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PQ': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PR': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PS': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PT': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PU': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PV': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PW': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'PX': {n:'Brazil', f:'🇧🇷', c:'SA'},
  'ZP': {n:'Paraguay', f:'🇵🇾', c:'SA'},
  'CE': {n:'Chile', f:'🇨🇱', c:'SA'},
  'CA': {n:'Chile', f:'🇨🇱', c:'SA'},
  'CB': {n:'Chile', f:'🇨🇱', c:'SA'},
  'CC': {n:'Chile', f:'🇨🇱', c:'SA'},
  'CD': {n:'Chile', f:'🇨🇱', c:'SA'},
  'LU': {n:'Argentina', f:'🇦🇷', c:'SA'},
  'AY': {n:'Argentina', f:'🇦🇷', c:'SA'},
  'AZ': {n:'Argentina', f:'🇦🇷', c:'SA'},
  'CX': {n:'Uruguay', f:'🇺🇾', c:'SA'},
  'CV': {n:'Uruguay', f:'🇺🇾', c:'SA'},
  'CW': {n:'Uruguay', f:'🇺🇾', c:'SA'},
  'OA': {n:'Peru', f:'🇵🇪', c:'SA'},
  'OB': {n:'Peru', f:'🇵🇪', c:'SA'},
  'OC': {n:'Peru', f:'🇵🇪', c:'SA'},
  'HK': {n:'Colombia', f:'🇨🇴', c:'SA'},
  'HJ': {n:'Colombia', f:'🇨🇴', c:'SA'},
  'YV': {n:'Venezuela', f:'🇻🇪', c:'SA'},
  'YW': {n:'Venezuela', f:'🇻🇪', c:'SA'},
  'YX': {n:'Venezuela', f:'🇻🇪', c:'SA'},
  'YY': {n:'Venezuela', f:'🇻🇪', c:'SA'},
  'HC': {n:'Ecuador', f:'🇪🇨', c:'SA'},

  // Asia
  'JA': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JE': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JF': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JG': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JH': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JI': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JJ': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JK': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JL': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JM': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JN': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JO': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JP': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JQ': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JR': {n:'Japan', f:'🇯🇵', c:'AS'},
  'JS': {n:'Japan', f:'🇯🇵', c:'AS'},
  '7J': {n:'Japan', f:'🇯🇵', c:'AS'},
  '7K': {n:'Japan', f:'🇯🇵', c:'AS'},
  '7L': {n:'Japan', f:'🇯🇵', c:'AS'},
  '7M': {n:'Japan', f:'🇯🇵', c:'AS'},
  '7N': {n:'Japan', f:'🇯🇵', c:'AS'},
  '8J': {n:'Japan', f:'🇯🇵', c:'AS'},
  '8N': {n:'Japan', f:'🇯🇵', c:'AS'},

  'HL': {n:'S. Korea', f:'🇰🇷', c:'AS'},
  'DS': {n:'S. Korea', f:'🇰🇷', c:'AS'},
  '6K': {n:'S. Korea', f:'🇰🇷', c:'AS'},
  '6L': {n:'S. Korea', f:'🇰🇷', c:'AS'},
  '6M': {n:'S. Korea', f:'🇰🇷', c:'AS'},
  '6N': {n:'S. Korea', f:'🇰🇷', c:'AS'},

  'BY': {n:'China', f:'🇨🇳', c:'AS'},
  'BA': {n:'China', f:'🇨🇳', c:'AS'},
  'BD': {n:'China', f:'🇨🇳', c:'AS'},
  'BG': {n:'China', f:'🇨🇳', c:'AS'},
  'BH': {n:'China', f:'🇨🇳', c:'AS'},
  'BI': {n:'China', f:'🇨🇳', c:'AS'},
  'BT': {n:'China', f:'🇨🇳', c:'AS'},
  'BZ': {n:'China', f:'🇨🇳', c:'AS'},
  'BV': {n:'Taiwan', f:'🇹🇼', c:'AS'},
  'VR': {n:'Hong Kong', f:'🇭🇰', c:'AS'},
  'XX': {n:'Macao', f:'🇲🇴', c:'AS'},

  'VU': {n:'India', f:'🇮🇳', c:'AS'},
  'AT': {n:'India', f:'🇮🇳', c:'AS'},
  '8T': {n:'India', f:'🇮🇳', c:'AS'},

  'HS': {n:'Thailand', f:'🇹🇭', c:'AS'},
  'E2': {n:'Thailand', f:'🇹🇭', c:'AS'},
  'XU': {n:'Cambodia', f:'🇰🇭', c:'AS'},
  'XV': {n:'Vietnam', f:'🇻🇳', c:'AS'},
  '3W': {n:'Vietnam', f:'🇻🇳', c:'AS'},
  'YB': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  'YC': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  'YD': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  'YE': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  'YF': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  'YG': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  'YH': {n:'Indonesia', f:'🇮🇩', c:'AS'},
  '9M': {n:'Malaysia', f:'🇲🇾', c:'AS'},
  '9V': {n:'Singapore', f:'🇸🇬', c:'AS'},
  '9W': {n:'Malaysia', f:'🇲🇾', c:'AS'},
  'DU': {n:'Philippines', f:'🇵🇭', c:'AS'},
  'DV': {n:'Philippines', f:'🇵🇭', c:'AS'},
  'DW': {n:'Philippines', f:'🇵🇭', c:'AS'},
  '4F': {n:'Philippines', f:'🇵🇭', c:'AS'},

  // Middle East
  '4X': {n:'Israel', f:'🇮🇱', c:'AS'},
  '4Z': {n:'Israel', f:'🇮🇱', c:'AS'},
  '9K': {n:'Kuwait', f:'🇰🇼', c:'AS'},
  'A4': {n:'Oman', f:'🇴🇲', c:'AS'},
  'A6': {n:'UAE', f:'🇦🇪', c:'AS'},
  'A7': {n:'Qatar', f:'🇶🇦', c:'AS'},
  'A9': {n:'Bahrain', f:'🇧🇭', c:'AS'},
  'HZ': {n:'Saudi Arabia', f:'🇸🇦', c:'AS'},
  '7Z': {n:'Saudi Arabia', f:'🇸🇦', c:'AS'},
  '8Z': {n:'Saudi Arabia', f:'🇸🇦', c:'AS'},
  'YI': {n:'Iraq', f:'🇮🇶', c:'AS'},
  'EK': {n:'Armenia', f:'🇦🇲', c:'AS'},
  '4J': {n:'Azerbaijan', f:'🇦🇿', c:'AS'},
  'JY': {n:'Jordan', f:'🇯🇴', c:'AS'},
  'OD': {n:'Lebanon', f:'🇱🇧', c:'AS'},

  // Africa
  'ZS': {n:'S. Africa', f:'🇿🇦', c:'AF'},
  'ZR': {n:'S. Africa', f:'🇿🇦', c:'AF'},
  'ZT': {n:'S. Africa', f:'🇿🇦', c:'AF'},
  'ZU': {n:'S. Africa', f:'🇿🇦', c:'AF'},
  'CN': {n:'Morocco', f:'🇲🇦', c:'AF'},
  '5C': {n:'Morocco', f:'🇲🇦', c:'AF'},
  '7X': {n:'Algeria', f:'🇩🇿', c:'AF'},
  '3V': {n:'Tunisia', f:'🇹🇳', c:'AF'},
  '5A': {n:'Libya', f:'🇱🇾', c:'AF'},
  'SU': {n:'Egypt', f:'🇪🇬', c:'AF'},
  '6W': {n:'Senegal', f:'🇸🇳', c:'AF'},
  '5X': {n:'Uganda', f:'🇺🇬', c:'AF'},
  '5Z': {n:'Kenya', f:'🇰🇪', c:'AF'},

  // Oceania
  'VK': {n:'Australia', f:'🇦🇺', c:'OC'},
  'AX': {n:'Australia', f:'🇦🇺', c:'OC'},
  'VK9': {n:'AUS islands', f:'🇦🇺', c:'OC'},
  'ZL': {n:'New Zealand', f:'🇳🇿', c:'OC'},
  'ZM': {n:'New Zealand', f:'🇳🇿', c:'OC'},
  'FK': {n:'New Caledonia', f:'🇳🇨', c:'OC'},
  'FO': {n:'French Polynesia', f:'🇵🇫', c:'OC'},
  'KH0': {n:'Marianas', f:'🇲🇵', c:'OC'},
  'KH2': {n:'Guam', f:'🇬🇺', c:'OC'},
  'KH8': {n:'American Samoa', f:'🇦🇸', c:'OC'},
  'V6': {n:'Micronesia', f:'🇫🇲', c:'OC'},
  'V7': {n:'Marshall Is.', f:'🇲🇭', c:'OC'},
  'T2': {n:'Tuvalu', f:'🇹🇻', c:'OC'},
  'T3': {n:'Kiribati', f:'🇰🇮', c:'OC'},
  'YJ': {n:'Vanuatu', f:'🇻🇺', c:'OC'},
  '3D2': {n:'Fiji', f:'🇫🇯', c:'OC'},
  'A3': {n:'Tonga', f:'🇹🇴', c:'OC'},

  // Rare DXCC — occasionally show up
  'JT': {n:'Mongolia', f:'🇲🇳', c:'AS'},
  'KP2': {n:'US Virgin Is.', f:'🇻🇮', c:'NA'},
  'ZB': {n:'Gibraltar', f:'🇬🇮', c:'EU'},
  'ZC4': {n:'UK Bases Cyprus', f:'🇬🇧', c:'AS'},
  'H4': {n:'Solomon Is.', f:'🇸🇧', c:'OC'},
  'V3': {n:'Belize', f:'🇧🇿', c:'NA'},
  'V4': {n:'St. Kitts', f:'🇰🇳', c:'NA'},
  'V5': {n:'Namibia', f:'🇳🇦', c:'AF'},
  'ZF': {n:'Cayman Is.', f:'🇰🇾', c:'NA'},
};

// Precompute a sorted list of keys (longest first - match priority)
const _SORTED_KEYS = Object.keys(DXCC_TABLE).sort((a, b) => b.length - a.length);

// OPERATING suffixes (mode of operation, NOT a different country) -
// "SP3MZM/P" is still Poland, not a separate DXCC. Filtered out BEFORE
// trying to resolve a genuinely compound callsign (two different
// countries) below.
const _OP_SUFFIXES = new Set(['P', 'M', 'MM', 'QRP', 'A', 'AM', 'MAR', 'LH', 'R']);

function _matchesTable(seg) {
  return _SORTED_KEYS.some(key => seg.startsWith(key));
}

// A compound callsign ("A/B") has NO fixed prefix/home-call ordering — in
// practice both orderings show up ("W1/DL3ABC" and "SP3MZM/W1"). The
// previous version always took the FIRST segment (split('/')[0]), so for
// "SP3MZM/W1" it showed Poland instead of the USA. Now: if only ONE
// segment matches the DXCC table, it's the location prefix; if BOTH match
// (typical - both look like real callsigns), the shorter one is usually
// the prefix (a home callsign is generally longer than a bare location prefix).
function _resolveBase(parts) {
  if (parts.length === 0) return ''; // e.g. the call was empty/just "/" - no segments
  if (parts.length === 1) return parts[0];
  const candidates = parts.filter(p => !_OP_SUFFIXES.has(p));
  if (candidates.length === 0) return parts[0];
  if (candidates.length === 1) return candidates[0];
  const matching = candidates.filter(_matchesTable);
  if (matching.length === 1) return matching[0];
  if (matching.length > 1) {
    return matching.reduce((a, b) => a.length <= b.length ? a : b);
  }
  return candidates[0];
}

// Lookup: try each prefix in turn, longest first
function lookup(call) {
  if (!call) return { name: '', prefix: '', flag: '', continent: '' };
  const up = call.toUpperCase().replace(/[<>]/g, '');
  const base = _resolveBase(up.split('/').filter(Boolean));
  // Try matches from longest to shortest
  for (const key of _SORTED_KEYS) {
    if (base.startsWith(key)) {
      const info = DXCC_TABLE[key];
      return {
        name: info.n,
        prefix: key,
        flag: info.f,
        continent: info.c,
      };
    }
  }
  return { name: '', prefix: '', flag: '', continent: '' };
}

window.DXCC = { lookup };
})();
