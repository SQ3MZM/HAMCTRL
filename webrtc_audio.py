#!/usr/bin/env python3
"""
webrtc_audio.py — odbior audio TX przez WebRTC (aiortc).
Klient (mikrofon lub VB-Cable) -> RTCPeerConnection -> AudioFrame (PCM)
  -> ten sam _tx_stream (PyAudio) co dotychczasowy pipeline WS.

Sygnalizacja (offer/answer/ICE) idzie przez istniejacy WebSocket (/ws),
wiec nie trzeba osobnego endpointu HTTP.

Tylko jeden klient nadaje na raz (ograniczenie sprzetowe IC-746/PTT),
wiec nie ma potrzeby mixowania — najnowsze polaczenie zastepuje poprzednie.
"""
import asyncio, fractions, struct
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaBlackhole

OPUS_RATE = 48000


class WebRTCAudioReceiver:
    """
    Zarzadza pojedynczym aktywnym RTCPeerConnection dla TX audio.
    Odebrane ramki PCM (mono, 48kHz, int16) przekazywane przez callback.
    """

    def __init__(self, on_pcm_frame, on_track_started=None, on_track_ended=None):
        """
        on_pcm_frame(pcm_bytes: bytes) — wywolywane dla kazdej ramki PCM
        on_track_started() / on_track_ended() — opcjonalne hooki (np. start_tx/stop_tx)
        """
        self._pc: RTCPeerConnection | None = None
        self._on_pcm = on_pcm_frame
        self._on_start = on_track_started
        self._on_end   = on_track_ended
        self._task: asyncio.Task | None = None
        self.active = False

    async def handle_offer(self, sdp: str, type_: str = "offer") -> dict:
        """
        Przyjmij SDP offer od klienta, zwroc SDP answer.
        Jesli istnieje poprzednie polaczenie — zamknij je (jeden nadawca naraz).
        """
        await self.close()

        self._frame_count = 0
        if hasattr(self, "_resampler"):
            del self._resampler

        config = RTCConfiguration(iceServers=[
            RTCIceServer(urls="stun:stun.l.google.com:19302")
        ])
        pc = RTCPeerConnection(configuration=config)
        self._pc = pc

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            print(f"[webrtc] Audio track odebrany: {track.kind}")
            self.active = True
            if self._on_start:
                self._on_start()
            self._task = asyncio.ensure_future(self._consume_track(track))

        @pc.on("connectionstatechange")
        async def on_state_change():
            print(f"[webrtc] Stan polaczenia: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self.close()

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            print(f"[webrtc] ICE stan: {pc.iceConnectionState}")

        @pc.on("icegatheringstatechange")
        async def on_ice_gathering_change():
            print(f"[webrtc] ICE gathering: {pc.iceGatheringState}")

        offer = RTCSessionDescription(sdp=sdp, type=type_)
        await pc.setRemoteDescription(offer)

        offer_candidates = sdp.count('a=candidate')
        print(f"[webrtc] Offer od klienta: {offer_candidates} kandydatow ICE")

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Czekaj az ICE gathering serwera sie zakonczy (max 3s)
        for _ in range(30):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        final_sdp = pc.localDescription.sdp
        answer_candidates = final_sdp.count('a=candidate')
        print(f"[webrtc] Answer serwera: {answer_candidates} kandydatow ICE, gathering={pc.iceGatheringState}")

        return {
            "sdp": final_sdp,
            "type": pc.localDescription.type,
        }

    async def add_ice_candidate(self, candidate: dict):
        """Dodaj ICE candidate od klienta (trickle ICE)."""
        if not self._pc:
            return
        raw = candidate.get("candidate", "")
        if not raw:
            return  # pusty kandydat = end-of-candidates, ignoruj
        try:
            from aioice import Candidate as IceCandidate
            # candidate string format: "candidate:foundation component protocol priority ip port typ type ..."
            ice_cand = IceCandidate.from_sdp(raw.replace("candidate:", "", 1))
            cand = RTCIceCandidate(
                component=ice_cand.component,
                foundation=ice_cand.foundation,
                ip=ice_cand.host,
                port=ice_cand.port,
                priority=ice_cand.priority,
                protocol=ice_cand.transport,
                type=ice_cand.type,
                relatedAddress=ice_cand.related_address,
                relatedPort=ice_cand.related_port,
                tcpType=ice_cand.tcptype,
                sdpMid=candidate.get("sdpMid"),
                sdpMLineIndex=candidate.get("sdpMLineIndex"),
            )
            await self._pc.addIceCandidate(cand)
            print(f"[webrtc] ICE candidate dodany: {ice_cand.host}:{ice_cand.port} typ={ice_cand.type}")
        except Exception as e:
            print(f"[webrtc] ICE candidate blad: {e} | raw={raw[:80]}")

    async def _consume_track(self, track):
        """Petla odbierajaca klatki audio i przekazujaca PCM do callbacku."""
        try:
            while True:
                frame = await track.recv()
                # frame to av.AudioFrame — konwertuj do PCM int16 mono 48kHz
                pcm = self._frame_to_pcm(frame)
                if pcm and self._on_pcm:
                    self._on_pcm(pcm)
        except Exception as e:
            print(f"[webrtc] Track zakonczony: {e}")
        finally:
            self.active = False
            if self._on_end:
                self._on_end()

    def _frame_to_pcm(self, frame) -> bytes:
        """Konwertuj av.AudioFrame -> PCM int16 mono @ 48kHz."""
        try:
            n_ch = len(frame.layout.channels)

            if frame.sample_rate == OPUS_RATE and frame.format.name == "s16":
                raw = bytes(frame.planes[0])
                expected_bytes = frame.samples * n_ch * 2
                if len(raw) != expected_bytes:
                    raw = raw[:expected_bytes]

                if n_ch == 1:
                    pcm = raw
                elif n_ch == 2:
                    try:
                        import numpy as np
                        stereo = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
                        mono = ((stereo[:,0].astype(np.int32) + stereo[:,1].astype(np.int32)) // 2).astype(np.int16)
                        pcm = mono.tobytes()
                    except ImportError:
                        n = len(raw) // 4
                        samples = struct.unpack(f"<{n*2}h", raw)
                        mono = [(samples[i*2]+samples[i*2+1])//2 for i in range(n)]
                        pcm = struct.pack(f"<{n}h", *mono)
                else:
                    pcm = raw
                return pcm

            # Inny sample rate - resampling konieczny (rzadki przypadek)
            from av import AudioResampler
            if not hasattr(self, "_resampler"):
                self._resampler = AudioResampler(format="s16", layout="mono", rate=OPUS_RATE)
            frames = self._resampler.resample(frame)
            pcm = b""
            for f in frames:
                pcm += bytes(f.planes[0])
            return pcm

        except Exception as e:
            print(f"[webrtc] frame_to_pcm blad: {e}")
            return b""

    async def close(self):
        """Zamknij aktywne polaczenie."""
        if self._task:
            self._task.cancel()
            self._task = None
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None
        self.active = False
