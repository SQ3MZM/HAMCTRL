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

  // Malta, San Marino, Monaco, Andorra, Vatican
  '9H': {n:'Malta', f:'🇲🇹', c:'EU'},
  'T7': {n:'San Marino', f:'🇸🇲', c:'EU'},
  '3A': {n:'Monaco', f:'🇲🇨', c:'EU'},
  'C3': {n:'Andorra', f:'🇦🇩', c:'EU'},
  'HV': {n:'Vatican', f:'🇻🇦', c:'EU'},

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

// Reverse index (country name -> flag/continent), built from the same
// table above - one source of truth. Used to render the flag from the
// COUNTRY NAME that QRZ.com/HamQTH already resolved and saved with the
// QSO (see qsolog.js), instead of re-guessing from the callsign prefix.
// That re-guess is what silently dropped the flag for entities missing
// from - or oddly split across - this deliberately simplified ~150-
// country table (e.g. Andorra was entirely absent until this list, and
// many countries' SPECIAL-EVENT callsign prefixes differ from their
// normal ham-radio prefixes and were never going to be covered here) -
// QRZ/HamQTH's own lookup already gets this right independent of the
// prefix, so prefer their answer once we have it.
const _NAME_TO_INFO = {};
for (const key of _SORTED_KEYS) {
  const info = DXCC_TABLE[key];
  const nameKey = info.n.toLowerCase();
  if (!(nameKey in _NAME_TO_INFO)) {
    _NAME_TO_INFO[nameKey] = { flag: info.f, continent: info.c };
  }
}

// There are 340+ current DXCC entities - the table above only spans the
// ~150 most commonly worked ones, by design (see the file header). Name
// matching against ONLY that table therefore still misses the flag for
// anything outside it (reported live: this happened for Andorra before
// it was added above, and will keep happening for any other DXCC entity
// not in that list). Flags are mechanically derivable from a plain
// ISO 3166-1 alpha-2 code (2 "regional indicator" chars) - so instead of
// hand-typing ~340 more flag emoji (error-prone, hard to review), this
// maps country names to their 2-letter code and COMPUTES the flag. Covers
// every standard country name QRZ/HamQTH are likely to return, plus a
// handful of common DXCC-only territories (Hawaii, Kaliningrad, etc.)
// that aren't independent ISO countries - those fall back to their
// parent country's flag, same convention other logging tools use.
function _flagFromISO2(iso2) {
  if (!iso2 || iso2.length !== 2) return '';
  const A = 0x1F1E6, base = 'A'.charCodeAt(0);
  return String.fromCodePoint(A + (iso2.charCodeAt(0) - base))
       + String.fromCodePoint(A + (iso2.charCodeAt(1) - base));
}

const _ISO2_NAMES = {
  // Europe
  albania:'AL', andorra:'AD', austria:'AT', belarus:'BY', belgium:'BE',
  'bosnia and herzegovina':'BA', bosnia:'BA', bulgaria:'BG', croatia:'HR',
  cyprus:'CY', 'czech republic':'CZ', czechia:'CZ', denmark:'DK',
  estonia:'EE', finland:'FI', france:'FR', germany:'DE', greece:'GR',
  hungary:'HU', iceland:'IS', ireland:'IE', italy:'IT', kosovo:'XK',
  latvia:'LV', liechtenstein:'LI', lithuania:'LT', luxembourg:'LU',
  malta:'MT', moldova:'MD', monaco:'MC', montenegro:'ME',
  netherlands:'NL', 'north macedonia':'MK', macedonia:'MK', norway:'NO',
  poland:'PL', portugal:'PT', romania:'RO', russia:'RU',
  'russian federation':'RU', 'san marino':'SM', serbia:'RS',
  slovakia:'SK', slovenia:'SI', spain:'ES', sweden:'SE',
  switzerland:'CH', ukraine:'UA', 'united kingdom':'GB', england:'GB',
  scotland:'GB', wales:'GB', 'northern ireland':'GB', vatican:'VA',
  'vatican city':'VA', 'faroe islands':'FO', greenland:'GL',
  gibraltar:'GI', guernsey:'GG', jersey:'JE', 'isle of man':'IM',
  svalbard:'SJ', 'aland islands':'AX', 'kaliningrad':'RU',
  'asiatic russia':'RU', 'european russia':'RU',

  // Asia
  afghanistan:'AF', armenia:'AM', azerbaijan:'AZ', bahrain:'BH',
  bangladesh:'BD', bhutan:'BT', brunei:'BN', cambodia:'KH', china:'CN',
  georgia:'GE', india:'IN', indonesia:'ID', iran:'IR', iraq:'IQ',
  israel:'IL', japan:'JP', jordan:'JO', kazakhstan:'KZ', kuwait:'KW',
  kyrgyzstan:'KG', laos:'LA', lebanon:'LB', malaysia:'MY',
  maldives:'MV', mongolia:'MN', myanmar:'MM', burma:'MM', nepal:'NP',
  'north korea':'KP', oman:'OM', pakistan:'PK', philippines:'PH',
  qatar:'QA', 'saudi arabia':'SA', singapore:'SG', 'south korea':'KR',
  korea:'KR', 'sri lanka':'LK', syria:'SY', taiwan:'TW',
  tajikistan:'TJ', thailand:'TH', 'timor-leste':'TL', turkey:'TR',
  turkmenistan:'TM', 'united arab emirates':'AE', uzbekistan:'UZ',
  vietnam:'VN', yemen:'YE', 'hong kong':'HK', macao:'MO', macau:'MO',

  // Africa
  algeria:'DZ', angola:'AO', benin:'BJ', botswana:'BW',
  'burkina faso':'BF', burundi:'BI', cameroon:'CM', 'cape verde':'CV',
  'central african republic':'CF', chad:'TD', comoros:'KM', congo:'CG',
  'democratic republic of the congo':'CD', djibouti:'DJ', egypt:'EG',
  'equatorial guinea':'GQ', eritrea:'ER', eswatini:'SZ', swaziland:'SZ',
  ethiopia:'ET', gabon:'GA', gambia:'GM', ghana:'GH', guinea:'GN',
  'guinea-bissau':'GW', 'ivory coast':'CI', "cote d'ivoire":'CI',
  kenya:'KE', lesotho:'LS', liberia:'LR', libya:'LY', madagascar:'MG',
  malawi:'MW', mali:'ML', mauritania:'MR', mauritius:'MU', morocco:'MA',
  mozambique:'MZ', namibia:'NA', niger:'NE', nigeria:'NG', rwanda:'RW',
  'sao tome and principe':'ST', senegal:'SN', seychelles:'SC',
  'sierra leone':'SL', somalia:'SO', 'south africa':'ZA',
  'south sudan':'SS', sudan:'SD', tanzania:'TZ', togo:'TG',
  tunisia:'TN', uganda:'UG', zambia:'ZM', zimbabwe:'ZW',
  'western sahara':'EH',

  // Americas
  argentina:'AR', bahamas:'BS', barbados:'BB', belize:'BZ',
  bolivia:'BO', brazil:'BR', canada:'CA', chile:'CL', colombia:'CO',
  'costa rica':'CR', cuba:'CU', dominica:'DM', 'dominican republic':'DO',
  ecuador:'EC', 'el salvador':'SV', grenada:'GD', guatemala:'GT',
  guyana:'GY', haiti:'HT', honduras:'HN', jamaica:'JM', mexico:'MX',
  nicaragua:'NI', panama:'PA', paraguay:'PY', peru:'PE',
  'saint kitts and nevis':'KN', 'saint lucia':'LC',
  'saint vincent and the grenadines':'VC', suriname:'SR',
  'trinidad and tobago':'TT', 'united states':'US', usa:'US',
  uruguay:'UY', venezuela:'VE', 'puerto rico':'PR',
  'us virgin islands':'VI', bermuda:'BM', 'cayman islands':'KY',
  aruba:'AW', curacao:'CW', 'sint maarten':'SX',
  'turks and caicos islands':'TC', 'british virgin islands':'VG',
  anguilla:'AI', 'antigua and barbuda':'AG', montserrat:'MS',
  'falkland islands':'FK', 'french guiana':'GF', guadeloupe:'GP',
  martinique:'MQ', 'saint pierre and miquelon':'PM',
  'saint barthelemy':'BL', 'saint martin':'MF',

  // Oceania
  australia:'AU', fiji:'FJ', kiribati:'KI', 'marshall islands':'MH',
  micronesia:'FM', nauru:'NR', 'new zealand':'NZ', palau:'PW',
  'papua new guinea':'PG', samoa:'WS', 'solomon islands':'SB',
  tonga:'TO', tuvalu:'TV', vanuatu:'VU', 'cook islands':'CK',
  'french polynesia':'PF', 'new caledonia':'NC', niue:'NU',
  'norfolk island':'NF', guam:'GU', 'northern mariana islands':'MP',
  'american samoa':'AS', 'wallis and futuna':'WF', pitcairn:'PN',

  // Common DXCC-only territories that share a parent ISO country -
  // no separate ISO2 code, shown with the parent's flag (same as most
  // other logging software does for these)
  hawaii:'US', alaska:'US',
};

function lookupByName(name) {
  if (!name) return { flag: '', continent: '' };
  const key = name.trim().toLowerCase();
  if (_NAME_TO_INFO[key]) return _NAME_TO_INFO[key];
  const iso2 = _ISO2_NAMES[key];
  if (iso2) return { flag: _flagFromISO2(iso2), continent: '' };
  return { flag: '', continent: '' };
}

window.DXCC = { lookup, lookupByName };
})();
