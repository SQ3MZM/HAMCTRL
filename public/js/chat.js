/**
 * chat.js (frontend) — operator chat, embedded in the OPERATORZY panel.
 *
 * The online-users list is RadioLock's job (radiolock.js) - it already
 * knows who holds the TRX, which this file's older version didn't. This
 * file only renders the message thread. History arrives via the 'chat_init'
 * WS snapshot sent right after 'init' on every (re)connect (same pattern
 * as auto_seq_status/ft8_rx_status in ws.js) - no REST endpoint, session
 * history only (webapp.py's App._chat_history is an in-memory ring
 * buffer, cleared on server restart).
 *
 * Message types:
 *   text   → normal user message
 *   system → system info (reserved for future use, e.g. joined/left)
 */
(function () {
  'use strict';

  const ROLE_COLORS = {
    admin:    '#f0b429',
    operator: '#4cdb6a',
    viewer:   'rgba(200,220,200,0.5)',
    system:   'rgba(76,219,106,0.35)',
  };

  // #m-chat-modal only exists on mobile.html - used below to tell the two
  // "shells" apart (mobile = full-screen modal + unread badge + no
  // drag-resize; desktop = always-visible panel with a CW-decoder-style
  // resize handle). The actual message rendering (appendMessage) is
  // identical either way.
  function _isMobile() { return !!document.getElementById('m-chat-modal'); }

  let _unreadCount = 0;

  function markRead() {
    _unreadCount = 0;
    const badge = document.getElementById('m-chat-badge');
    if (badge) { badge.hidden = true; badge.textContent = '0'; }
  }

  // ── WS handler (called from ws.js's default dispatch branch, and from
  // mobile.js's own dispatcher on mobile) ─────────────────────────────────
  function handleWS(msg) {
    if (msg.type === 'chat_init') {
      const box = document.getElementById('chat-messages');
      if (box) { box.innerHTML = ''; box.scrollTop = 0; }
      // Newest-first (see appendMessage) - processing history in its
      // stored oldest-to-newest order with prepend-at-top naturally
      // ends up with the newest message on top, no reversal needed.
      (msg.history || []).forEach(m => appendMessage(m, false));
      return;
    }
    if (msg.type === 'chat_message') {
      appendMessage(msg.message, true);
      // Unread badge (mobile only - the modal has to be closed for a
      // message to actually be "unread"; on desktop the panel is always
      // visible so there's nothing to badge).
      const modal = document.getElementById('m-chat-modal');
      if (modal && modal.style.display === 'none') {
        _unreadCount++;
        const badge = document.getElementById('m-chat-badge');
        if (badge) { badge.hidden = false; badge.textContent = String(_unreadCount); }
      }
      return;
    }
  }

  // ── Render messages ───────────────────────────────────────────────────────
  function appendMessage(msg, scroll) {
    const box = document.getElementById('chat-messages');
    if (!box) return;

    const isSystem = msg.type === 'system';
    const isMine   = msg.username === window.CurrentUser?.username;
    const t        = new Date(msg.timestamp);
    const timeStr  = `${t.getHours().toString().padStart(2,'0')}:${t.getMinutes().toString().padStart(2,'0')}`;
    const color    = ROLE_COLORS[msg.role] || ROLE_COLORS.viewer;

    const el = document.createElement('div');
    el.className = 'chat-msg' + (isSystem ? ' chat-system' : '') + (isMine ? ' chat-mine' : '');

    if (isSystem) {
      el.innerHTML = `<span class="chat-time">${timeStr}</span><span class="chat-sys-text">${esc(msg.text)}</span>`;
    } else {
      const badge   = msg.callsign ? `<span class="chat-call">${esc(msg.callsign)}</span>` : '';
      const roleDot = `<span class="chat-role-dot" style="background:${color}" title="${esc(msg.role)}"></span>`;
      el.innerHTML  = `
        <div class="chat-header">
          ${roleDot}
          <span class="chat-user" style="color:${color}">${esc(msg.username)}</span>
          ${badge}
          <span class="chat-time">${timeStr}</span>
        </div>
        <div class="chat-text">${esc(msg.text)}</div>`;
    }

    // Newest-first (2026-09-03, live report: "najnowsze informacje
    // powinny byc od gory... zaraz znikna" - the chat box is small by
    // design (see the resize handle below), so with newest-at-bottom the
    // operator had to actively scroll down every time just to see what
    // was just said, and a message could scroll out of the tiny visible
    // area within moments). Prepend instead of append - processing
    // history in its normal oldest-to-newest order with insertBefore at
    // the front naturally ends up newest-on-top, no need to reverse the
    // array first.
    box.insertBefore(el, box.firstChild);

    // Cap the number of message elements kept in the DOM (independent of
    // the server's own _CHAT_HISTORY_MAX ring buffer - this just bounds
    // how much the browser has to keep rendered). Oldest are now at the
    // BOTTOM, so trim from there instead of the top.
    while (box.children.length > 150) box.removeChild(box.lastChild);

    // A new live message is already inserted at the top, i.e. right where
    // the box is scrolled to by default - only actually needed if the
    // operator had scrolled down to read older messages.
    if (scroll) box.scrollTop = 0;
  }

  // ── Send message ──────────────────────────────────────────────────────────
  function send() {
    const inp  = document.getElementById('chat-input');
    const text = inp?.value.trim();
    if (!text) return;
    window.WS?.send({ type: 'chat_send', text });
    inp.value = '';
    inp.focus();
  }

  function esc(str) {
    return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Window resize (persisted height, same pattern as the CW decoder -
  // see deepcw.js's _initCwScaling) - DESKTOP ONLY. Skipped on mobile:
  // touch has no drag-resize handle to use anyway, and desktop/mobile
  // share the SAME origin (and therefore the SAME localStorage) - if this
  // ran on both, a height dragged on desktop would leak in as an
  // inappropriate inline pixel height on the phone's modal, and vice
  // versa. Mobile's height comes from CSS (.m-chat-messages) instead. ──
  function _initResize() {
    if (_isMobile()) return;
    const el = document.getElementById('chat-messages');
    if (!el) return;
    try {
      const h = localStorage.getItem('chat_messages_h');
      if (h) el.style.height = h;
    } catch (e) {}
    el.style.resize = 'vertical';
    el.style.overflow = 'auto';
    try {
      let _saveTimer = null;
      const ro = new ResizeObserver(() => {
        clearTimeout(_saveTimer);
        _saveTimer = setTimeout(() => {
          try { localStorage.setItem('chat_messages_h', el.style.height || el.offsetHeight + 'px'); } catch (e) {}
        }, 400);
      });
      ro.observe(el);
    } catch (e) { /* No ResizeObserver - resize still works, just not remembered */ }
  }

  // ── Export ───────────────────────────────────────────────────────────────
  window.Chat = { handleWS, send, markRead };

  // Hook up Enter in the input field + restore the remembered window height.
  document.addEventListener('DOMContentLoaded', () => {
    const inp = document.getElementById('chat-input');
    if (inp) {
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      });
    }
    _initResize();
  });

})();
