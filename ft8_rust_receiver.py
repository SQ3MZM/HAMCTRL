"""
ft8_rust_receiver.py — Receives FT8/FT4 decode results from Rust (ham_audio.exe)
over TCP port 9444 and hands them to webapp.py via an asyncio queue.

Architecture:
  ham_audio.exe → TCP 9444 → ft8_rust_receiver → asyncio.Queue → webapp.py

Message format from Rust (one line of JSON):
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
    Listens on a TCP port (Rust connects as the CLIENT, Python is the SERVER).
    Rust connects automatically once decode_loop starts.
    """

    def __init__(self, port: int = DECODE_PORT):
        self.port = port
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._server = None
        self._enabled = False
        self._mode = "FT8"

    async def start(self):
        """Starts the TCP server that listens for decode results from Rust."""
        # reuse_address=True: lets us take over a port stuck in TIME_WAIT from
        # a previous run (typical on server restart) instead of failing with
        # "address already in use".
        try:
            self._server = await asyncio.start_server(
                self._handle_rust_connection,
                host="127.0.0.1",
                port=self.port,
                reuse_address=True,
            )
        except OSError as e:
            # Port still held by a LIVE process (not TIME_WAIT) — usually an
            # old ham_audio.exe/Python process wasn't killed. Instead of
            # crashing the whole server, log it and let the rest keep running
            # (audio won't come up, but radio control/waterfall still will).
            print(f"[ft8rx] WARNING: port {self.port} in use ({e}). "
                  f"Kill the old ham_audio.exe/Python process and restart. "
                  f"FT8 decode is inactive.", flush=True)
            self._server = None
            return
        addr = self._server.sockets[0].getsockname()
        print(f"[ft8rx] Rust decoder receiver on {addr}", flush=True)

    async def _handle_rust_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info('peername')
        print(f"[ft8rx] Rust connected from {peer}", flush=True)
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
                if msg.get("type") == "heartbeat":
                    continue  # ignore heartbeat
                # Diagnostic types (startup_stats/decode_stats/pass_stats) go
                # to the queue REGARDLESS of self._enabled - they're just log
                # info (rayon_threads, timing), not a decode result.
                # startup_stats fires ONCE, right after the TCP connection is
                # made - if self._enabled hasn't been set to True yet at that
                # point (a race with server startup), the old code silently
                # dropped this line forever (the connection is one-shot per
                # ham_audio.exe run) - hence "nothing shows up" even though
                # Rust definitely sent it.
                is_diag = msg.get("type") in ("startup_stats", "decode_stats", "pass_stats")
                if not self._enabled and not is_diag:
                    continue
                try:
                    self._queue.put_nowait(msg)
                except asyncio.QueueFull:
                    pass  # drop when the queue is full
        except asyncio.IncompleteReadError:
            pass
        finally:
            print(f"[ft8rx] Rust disconnected {peer}", flush=True)
            writer.close()

    async def get_decode(self) -> dict | None:
        """Fetch one decode result. Waits up to 0.1s."""
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
        """Closes the server and WAITS for port 9444 to be fully released.
        Important on ham_audio restart - the new receiver must get a clean
        port, otherwise the old one keeps grabbing Rust's connection and no
        decodes get through."""
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # Clear the queue left over from the old connection
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except Exception:
            pass
