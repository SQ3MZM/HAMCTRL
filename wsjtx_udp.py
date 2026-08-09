#!/usr/bin/env python3
"""
wsjtx_udp.py — odbiornik UDP WSJT-X
=====================================
WSJT-X wysyła pakiety UDP (port 2237) z dekodowaniami FT8/FT4/JT65.
Ten moduł nasłuchuje UDP, parsuje pakiety i broadcastuje przez WebSocket
do wszystkich połączonych klientów przeglądarki.

Konfiguracja WSJT-X (na komputerze przy radiu):
  Settings → Reporting → UDP Server: localhost (lub 127.0.0.1)
  Settings → Reporting → UDP port:   2237
  Settings → Reporting → ✓ Accept UDP requests

Protokół: https://sourceforge.net/p/wsjt/wsjtx/ci/master/tree/NetworkMessage.hpp
"""

import asyncio
import struct
import socket
from typing import Callable, Awaitable

# ── Typy pakietów WSJT-X ─────────────────────────────────────────────────────
MSG_HEARTBEAT   = 0
MSG_STATUS      = 1
MSG_DECODE      = 2
MSG_CLEAR       = 3
MSG_QSO_LOGGED  = 5
MSG_CLOSE       = 6

MAGIC  = 0xADBCCBDA
SCHEMA = 2

# ── Parser ─────────────────────────────────────────────────────────────────────

def _read_u8(buf, pos):
    return struct.unpack_from('>B', buf, pos)[0], pos + 1

def _read_u32(buf, pos):
    return struct.unpack_from('>I', buf, pos)[0], pos + 4

def _read_i32(buf, pos):
    return struct.unpack_from('>i', buf, pos)[0], pos + 4

def _read_bool(buf, pos):
    v, pos = _read_u8(buf, pos)
    return bool(v), pos

def _read_f64(buf, pos):
    return struct.unpack_from('>d', buf, pos)[0], pos + 8

def _read_str(buf, pos):
    """Qt string: 4 bajty długość (0xFFFFFFFF = null), potem UTF-8."""
    length, pos = _read_u32(buf, pos)
    if length == 0xFFFFFFFF:
        return '', pos
    s = buf[pos:pos + length].decode('utf-8', errors='replace')
    return s, pos + length

def _read_qtime(buf, pos):
    """QTime: ms od północy."""
    ms, pos = _read_u32(buf, pos)
    h  = ms // 3600000
    m  = (ms % 3600000) // 60000
    s  = (ms % 60000) // 1000
    return f"{h:02d}{m:02d}{s:02d}", pos


def parse_packet(data: bytes) -> dict | None:
    """Parsuj pakiet UDP WSJT-X. Zwróć dict lub None jeśli błąd."""
    try:
        if len(data) < 8:
            return None

        magic, pos  = _read_u32(data, 0)
        schema, pos = _read_u32(data, pos)
        if magic != MAGIC:
            return None

        msg_type, pos = _read_u32(data, pos)
        _id, pos      = _read_str(data, pos)   # ID stacji WSJT-X

        # ── Heartbeat ─────────────────────────────────────────────────────────
        if msg_type == MSG_HEARTBEAT:
            max_schema, pos = _read_u32(data, pos)
            version, pos    = _read_str(data, pos)
            revision, pos   = _read_str(data, pos)
            return {
                "type":    "wsjtx_status",
                "running": True,
                "id":      _id,
                "version": version,
                "text":    f"WSJT-X {version} połączony",
            }

        # ── Status ────────────────────────────────────────────────────────────
        if msg_type == MSG_STATUS:
            freq, pos      = _read_u32(data, pos)  # Hz (tylko int, nie f64!)
            mode, pos      = _read_str(data, pos)
            dx_call, pos   = _read_str(data, pos)
            report, pos    = _read_str(data, pos)
            tx_mode, pos   = _read_str(data, pos)
            tx_enabled,pos = _read_bool(data, pos)
            transmit, pos  = _read_bool(data, pos)
            decoding, pos  = _read_bool(data, pos)
            rx_df, pos     = _read_u32(data, pos)
            tx_df, pos     = _read_u32(data, pos)
            de_call, pos   = _read_str(data, pos)
            de_grid, pos   = _read_str(data, pos)
            dx_grid, pos   = _read_str(data, pos)
            return {
                "type":       "wsjtx_status",
                "running":    True,
                "freq":       freq,
                "mode":       mode,
                "txMode":     tx_mode,
                "dxCall":     dx_call,
                "deCall":     de_call,
                "deGrid":     de_grid,
                "txEnabled":  tx_enabled,
                "transmit":   transmit,
                "decoding":   decoding,
                "rxDF":       rx_df,
                "txDF":       tx_df,
                "text":       f"{'📡 TX' if transmit else '📻 RX'} {mode} {freq/1e6:.3f}MHz",
            }

        # ── Dekodowanie ───────────────────────────────────────────────────────
        if msg_type == MSG_DECODE:
            is_new, pos     = _read_bool(data, pos)
            time_ms, pos    = _read_u32(data, pos)
            snr, pos        = _read_i32(data, pos)
            delta_t, pos    = _read_f64(data, pos)
            delta_f, pos    = _read_u32(data, pos)
            mode_str, pos   = _read_str(data, pos)
            message, pos    = _read_str(data, pos)
            low_conf, pos   = _read_bool(data, pos)

            h  = time_ms // 3600000
            m  = (time_ms % 3600000) // 60000
            s  = (time_ms % 60000) // 1000
            time_str = f"{h:02d}{m:02d}{s:02d}"

            return {
                "type":      "wsjtx_decode",
                "isNew":     is_new,
                "timeStr":   time_str,
                "snr":       snr,
                "deltaTime": round(delta_t, 1),
                "deltaFreq": delta_f,
                "mode":      mode_str,
                "message":   message,
                "lowConf":   low_conf,
            }

        # ── Wyczyść ───────────────────────────────────────────────────────────
        if msg_type == MSG_CLEAR:
            return {"type": "wsjtx_clear"}

        # ── QSO zalogowane ────────────────────────────────────────────────────
        if msg_type == MSG_QSO_LOGGED:
            # Wg WSJT-X NetworkMessage.hpp QSOLogged:
            # DateTimeOff(QDateTime), Frequency(u64/u32), DXCall, DXGrid,
            # TxPower, Comments, Name, DateTimeOn, OperatorCall,
            # MyCall, MyGrid, ExchangeSent, ExchangeRcvd [, PropMode, Adif]
            _dt_off, pos  = _read_u32(data, pos)   # DateTimeOff (msec) — pomijamy
            _dt_off2, pos = _read_u32(data, pos)   # DateTimeOff (utcOffset) — pomijamy
            freq, pos     = _read_u32(data, pos)   # Frequency Hz
            mode, pos     = _read_str(data, pos)   # Mode (FT8/FT4/JT65...)
            dx_call, pos  = _read_str(data, pos)   # DXCall
            dx_grid, pos  = _read_str(data, pos)   # DXGrid
            tx_power, pos = _read_str(data, pos)   # TxPower
            comments, pos = _read_str(data, pos)   # Comments
            name, pos     = _read_str(data, pos)   # Name
            # DateTimeOn
            _dt_on, pos   = _read_u32(data, pos)
            _dt_on2, pos  = _read_u32(data, pos)
            op_call, pos  = _read_str(data, pos)   # OperatorCall
            my_call, pos  = _read_str(data, pos)   # MyCall
            my_grid, pos  = _read_str(data, pos)   # MyGrid
            # ExchangeSent i ExchangeRcvd = raporty sygnalu (np. "-12", "+05")
            rst_sent, pos = _read_str(data, pos)   # ExchangeSent
            rst_rcvd, pos = _read_str(data, pos)   # ExchangeRcvd
            return {
                "type":     "wsjtx_qso_logged",
                "dxCall":   dx_call,
                "dxGrid":   dx_grid,
                "freq":     freq,
                "mode":     mode,
                "rstSent":  rst_sent,   # np. "-12 dB"
                "rstRcvd":  rst_rcvd,   # np. "+05 dB"
                "myCall":   my_call,
                "myGrid":   my_grid,
                "txPower":  tx_power,
                "comments": comments,
            }

        # ── Close ─────────────────────────────────────────────────────────────
        if msg_type == MSG_CLOSE:
            return {"type": "wsjtx_status", "running": False, "text": "WSJT-X rozłączony"}

    except Exception as e:
        pass  # uszkodzony pakiet — ignoruj

    return None


# ── Serwer UDP ────────────────────────────────────────────────────────────────

class WsjtxUdpServer:
    """Nasłuchuje UDP od WSJT-X i broadcastuje przez WebSocket."""

    def __init__(self, broadcast_fn: Callable):
        """
        broadcast_fn: async funkcja(dict) → wysyła do wszystkich WS klientów
        """
        self.broadcast  = broadcast_fn
        self._transport = None
        self._running   = False
        self._port      = 2237
        self.packets_rx = 0
        self.decodes_rx = 0

    def is_running(self) -> bool:
        return self._running

    async def start(self, port: int = 2237, host: str = "0.0.0.0") -> bool:
        """Uruchom nasłuchiwanie UDP."""
        if self._running:
            await self.stop()

        self._port = port
        loop = asyncio.get_running_loop()

        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self._on_packet),
                local_addr=(host, port),
            )
            self._transport = transport
            self._running   = True
            print(f"[wsjtx] UDP nasłuchuje na {host}:{port}")
            print(f"[wsjtx] WSJT-X: Settings → Reporting → UDP Server: localhost, Port: {port}")
            return True

        except OSError as e:
            print(f"[wsjtx] UDP błąd: {e}")
            if e.errno == 10048 or e.errno == 98:  # port zajęty
                print(f"[wsjtx] Port {port} zajęty — sprawdź czy inny program nie używa UDP {port}")
            return False

    async def stop(self):
        """Zatrzymaj nasłuchiwanie."""
        if self._transport:
            self._transport.close()
            self._transport = None
        self._running = False
        print("[wsjtx] UDP zatrzymany")

    def _on_packet(self, data: bytes, addr):
        """Callback z protokołu UDP — parsuj i broadcastuj."""
        self.packets_rx += 1
        msg = parse_packet(data)
        if msg:
            if msg["type"] == "wsjtx_decode":
                self.decodes_rx += 1
            asyncio.create_task(self.broadcast(msg))

    def get_status(self) -> dict:
        return {
            "running":    self._running,
            "port":       self._port,
            "packets_rx": self.packets_rx,
            "decodes_rx": self.decodes_rx,
        }


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self._cb = callback

    def datagram_received(self, data, addr):
        self._cb(data, addr)

    def error_received(self, exc):
        print(f"[wsjtx] UDP protokół błąd: {exc}")
