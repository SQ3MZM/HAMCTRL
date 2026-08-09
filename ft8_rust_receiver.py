"""
ft8_rust_receiver.py — Odbiera wyniki dekodowania FT8/FT4 z Rust (ham_audio.exe)
przez TCP port 9444 i przekazuje do webapp.py przez asyncio queue.

Architektura:
  ham_audio.exe → TCP 9444 → ft8_rust_receiver → asyncio.Queue → webapp.py

Format wiadomości z Rust (jedna linia JSON):
  {"type":"wsjtx_decode","timeStr":"183045","snr":-12,"deltaTime":0.2,
   "deltaFreq":1234,"message":"CQ XX0XXX JO72","call_to":"CQ",
   "call_de":"XX0XXX","report_or_grid":"JO72","mode":"FT8"}
"""
import asyncio
import json
import time
import logging

log = logging.getLogger(__name__)

DECODE_PORT = 9444
RECONNECT_DELAY = 3.0


class Ft8RustReceiver:
    """
    Nasluchuje na porcie TCP (Rust łączy się jako KLIENT, Python jest SERWEREM).
    Rust połączy się automatycznie gdy decode_loop wystartuje.
    """

    def __init__(self, port: int = DECODE_PORT):
        self.port = port
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._server = None
        self._enabled = False
        self._mode = "FT8"

    async def start(self):
        """Uruchamia TCP server nasłuchujący na wyniki decode z Rust."""
        # reuse_address=True: pozwala przejac port ktory jest w TIME_WAIT po
        # poprzednim uruchomieniu (typowe przy restarcie serwera) zamiast
        # padac z "address already in use".
        try:
            self._server = await asyncio.start_server(
                self._handle_rust_connection,
                host="127.0.0.1",
                port=self.port,
                reuse_address=True,
            )
        except OSError as e:
            # Port wciaz zajety przez ZYWY proces (nie TIME_WAIT). Zwykle stary
            # ham_audio.exe/Python nie zostal ubity. Zamiast wywalac caly
            # serwer - zaloguj i pozwol reszcie dzialac (audio nie wstanie, ale
            # sterowanie radiem/waterfall tak).
            print(f"[ft8rx] UWAGA: port {self.port} zajety ({e}). "
                  f"Zabij stary ham_audio.exe/Python i zrestartuj. "
                  f"FT8 decode nieaktywny.", flush=True)
            self._server = None
            return
        addr = self._server.sockets[0].getsockname()
        print(f"[ft8rx] Rust decoder receiver na {addr}", flush=True)

    async def _handle_rust_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info('peername')
        print(f"[ft8rx] Rust połączony z {peer}", flush=True)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not self._enabled:
                    continue
                if msg.get("type") == "heartbeat":
                    continue  # ignoruj heartbeat
                try:
                    self._queue.put_nowait(msg)
                except asyncio.QueueFull:
                    pass  # Pomiń gdy kolejka pełna
        except asyncio.IncompleteReadError:
            pass
        finally:
            print(f"[ft8rx] Rust rozłączony {peer}", flush=True)
            writer.close()

    async def get_decode(self) -> dict | None:
        """Pobierz jeden wynik dekodowania. Czeka do 0.1s."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    def enable(self, enabled: bool):
        self._enabled = enabled
        print(f"[ft8rx] RX enabled: {enabled}", flush=True)

    def set_mode(self, mode: str):
        self._mode = mode
        print(f"[ft8rx] Mode: {mode}", flush=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    def close(self):
        if self._server:
            self._server.close()

    async def stop(self):
        """Zamyka serwer i CZEKA na pelne zwolnienie portu 9444.
        Wazne przy restarcie ham_audio - nowy receiver musi dostac czysty port,
        inaczej stary przechwytuje polaczenie Rusta a dekodowania nie docieraja."""
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # Wyczysc kolejke po starym polaczeniu
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except Exception:
            pass
