/* theme.js — dzien/noc (jasny/ciemny motyw), approved 2026-10-15.
 *
 * Same persistence pattern as i18n.js's PL/EN toggle: localStorage
 * 'ham_theme', per-browser, default 'dark' (obecny wyglad dla kogos kto
 * nigdy nie kliknal przelacznika). Przelacza TYLKO neutralne kolory
 * tla/tekstu przez atrybut data-theme na <html> - sama paleta (jasna i
 * ciemna wersja) jest zdefiniowana w style.css jako CSS custom properties,
 * tutaj tylko ustawiamy ktora wersja jest aktywna.
 *
 * Anti-flash: index.html ma w <head> maly inline <script> ktory ustawia
 * data-theme NATYCHMIAST (przed zaladowaniem reszty strony), zeby nie bylo
 * widac błysku złego motywu przy starcie - ten plik tylko aktualizuje
 * ikonke przycisku i obsluguje klikniecie.
 */
const Theme = (() => {
  function detectInitial() {
    const saved = localStorage.getItem('ham_theme');
    if (saved === 'light' || saved === 'dark') return saved;
    return 'dark';
  }

  let current = detectInitial();

  function updateIcon() {
    const btn = document.getElementById('theme-switch');
    if (btn) btn.textContent = current === 'light' ? '☀️' : '🌙';
  }

  function apply(theme) {
    current = theme;
    document.documentElement.setAttribute('data-theme', theme);
    updateIcon();
  }

  function setTheme(theme) {
    apply(theme);
    localStorage.setItem('ham_theme', theme);
  }

  function toggle() {
    setTheme(current === 'dark' ? 'light' : 'dark');
  }

  // Apply immediately (not just update the icon) - belt-and-braces in case
  // the anti-flash inline <script> in <head> (index.html) didn't stick.
  apply(current);
  document.addEventListener('DOMContentLoaded', updateIcon);

  // Self-healing (live-confirmed need via browser automation, 2026-10-15):
  // something outside this app's own code - a browser extension touching
  // documentElement attributes, observed live via MutationObserver as a
  // single data-theme removal ~500ms after navigation, alongside an
  // unrelated "data-adreal-did" attribute neither this app nor its users
  // ever add - can strip data-theme off <html> shortly after load. Watch
  // for that and silently restore the correct value if it ever happens,
  // rather than leaving the page stuck showing the wrong (default CSS)
  // theme with no visible indication anything's wrong.
  new MutationObserver(() => {
    if (document.documentElement.getAttribute('data-theme') !== current) {
      document.documentElement.setAttribute('data-theme', current);
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  return { toggle, setTheme, get current() { return current; } };
})();
