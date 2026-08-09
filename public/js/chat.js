/**
 * chat.js (frontend) — czat operatorów + lista online
 *
 * Layout:
 *   [Lista online | Okno wiadomości + pole wpisywania]
 *
 * Typy wiadomości:
 *   text    → normalna wiadomość użytkownika
 *   system  → info systemowe (dołączył/opuścił)
 *   qso_alert → alert nowej łączności
 */
(function () {
  'use strict';

  const ROLE_COLORS = {
    admin:    '#f0b429',
    operator: '#4cdb6a',
    viewer:   'rgba(200,220,200,0.5)',
    system:   'rgba(76,219,106,0.35)',
  };

  let _rendered = false;

  // ── Init (ładowanie historii) ─────────────────────────────────────────────
  async function init() {
    if (!document.getElementById('chat-messages')) return;
    try {
      const r  = await fetch('/api/chat/history');
      const msgs = await r.json();
      msgs.forEach(m => appendMessage(m, false));
      scrollBottom();
      loadOnline();
    } catch (e) { console.warn('[chat] init error', e); }
    _rendered = true;
  }

  async function loadOnline() {
    try {
      const r = await fetch('/api/chat/online');
      const users = await r.json();
      renderOnline(users);
    } catch (e) {}
  }

  // ── WS handler (wywoływany z ws.js) ──────────────────────────────────────
  function handleWS(msg) {
    if (msg.type === 'chat_init') {
      const box = document.getElementById('chat-messages');
      if (box) box.innerHTML = '';
      (msg.history || []).forEach(m => appendMessage(m, false));
      scrollBottom();
      renderOnline(msg.online || []);
      return;
    }
    if (msg.type === 'chat_message') {
      appendMessage(msg.message, true);
      return;
    }
    if (msg.type === 'online_update') {
      renderOnline(msg.online || []);
      return;
    }
    if (msg.type === 'qso_new') {
      appendMessage({
        type: 'qso_alert',
        username: 'system',
        callsign: '',
        text: `📻 Nowe QSO: ${msg.entry?.callsign} — ${msg.entry?.band} ${msg.entry?.mode}`,
        timestamp: new Date().toISOString(),
      }, true);
    }
  }

  // ── Render wiadomości ─────────────────────────────────────────────────────
  function appendMessage(msg, scroll) {
    const box = document.getElementById('chat-messages');
    if (!box) return;

    const isSystem = msg.type === 'system' || msg.type === 'qso_alert';
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
      const roleDot = `<span class="chat-role-dot" style="background:${color}" title="${msg.role}"></span>`;
      el.innerHTML  = `
        <div class="chat-header">
          ${roleDot}
          <span class="chat-user" style="color:${color}">${esc(msg.username)}</span>
          ${badge}
          <span class="chat-time">${timeStr}</span>
        </div>
        <div class="chat-text">${esc(msg.text)}</div>`;
    }

    box.appendChild(el);

    // Ogranicz liczbę wiadomości w DOM
    while (box.children.length > 120) box.removeChild(box.firstChild);

    if (scroll) scrollBottom();
  }

  function scrollBottom() {
    const box = document.getElementById('chat-messages');
    if (box) box.scrollTop = box.scrollHeight;
  }

  // ── Render online users ───────────────────────────────────────────────────
  function renderOnline(users) {
    const el = document.getElementById('chat-online-list');
    if (!el) return;

    const cnt = document.getElementById('chat-online-count');
    if (cnt) cnt.textContent = users.length;

    if (!users.length) {
      el.innerHTML = `<div style="padding:10px;font-family:var(--mono);font-size:10px;color:var(--dim);text-align:center;">Brak online</div>`;
      return;
    }

    el.innerHTML = users.map(u => {
      const color = ROLE_COLORS[u.role] || ROLE_COLORS.viewer;
      const since = new Date(u.since);
      const sinceStr = `${since.getHours().toString().padStart(2,'0')}:${since.getMinutes().toString().padStart(2,'0')}`;
      return `
        <div class="online-item">
          <span class="online-dot" style="background:${color};box-shadow:0 0 5px ${color}"></span>
          <div class="online-info">
            <span class="online-name" style="color:${color}">${esc(u.username)}</span>
            ${u.callsign ? `<span class="online-call">${esc(u.callsign)}</span>` : ''}
          </div>
          <span class="online-since">${sinceStr}</span>
        </div>`;
    }).join('');
  }

  // ── Wyślij wiadomość ──────────────────────────────────────────────────────
  function send() {
    const inp  = document.getElementById('chat-input');
    const text = inp?.value.trim();
    if (!text) return;

    // Wyślij przez WS (szybsze)
    if (window.WS?.isConnected()) {
      WS.send({ type: 'chat_send', text });
    } else {
      fetch('/api/chat/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
    }
    inp.value = '';
    inp.focus();
  }

  function esc(str) {
    return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Eksport ───────────────────────────────────────────────────────────────
  window.Chat = { init, handleWS, send, loadOnline };

  // Podepnij Enter w polu wpisywania
  document.addEventListener('DOMContentLoaded', () => {
    const inp = document.getElementById('chat-input');
    if (inp) {
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      });
    }
  });

})();
