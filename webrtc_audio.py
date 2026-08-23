#!/usr/bin/env python3
"""
webrtc_audio.py — receives TX audio over WebRTC (aiortc).
Client (microphone or VB-Cable) -> RTCPeerConnection -> AudioFrame (PCM)
  -> the same _tx_stream (PyAudio) used by the existing WS pipeline.

Signaling (offer/answer/ICE) goes over the existing WebSocket (/ws), so no
separate HTTP endpoint is needed.

Only one client transmits at a time (IC-746/PTT hardware limitation), so
there's no need to mix — the newest connection replaces the previous one.
"""
import asyncio, fractions, socket, struct, time
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaBlackhole

OPUS_RATE = 48000

_STUN_HOST = "stun.l.google.com"
_STUN_PORT = 19302
_stun_ip_cache: str | None = None


async def _stun_url() -> str:
    """
    Resolve the STUN server hostname once and cache the IP.

    Mobile TX opens a brand-new RTCPeerConnection on EVERY PTT press (see
    mobile_audio.js header — a deliberate choice to avoid holding the mic
    open). aioice's gather_candidates() does a blocking socket.gethostbyname()
    on the STUN hostname for every single ICE gathering pass, so without
    caching, every PTT press pays a fresh DNS round-trip on top of the STUN
    UDP round-trip. Reported live as noticeable TX start delay. Resolving
    once and reusing the numeric IP removes that repeated cost; falls back
    to the hostname if resolution fails so behavior is unchanged in the
    failure case. Run via asyncio.to_thread (not a plain blocking call) —
    this shares the process with civ.py's CI-V polling loop on the same
    event loop, and a blocking DNS lookup right on the loop would stall
    that polling for however long the lookup takes.
    """
    global _stun_ip_cache
    if _stun_ip_cache is None:
        try:
            _stun_ip_cache = await asyncio.to_thread(socket.gethostbyname, _STUN_HOST)
        except OSError:
            _stun_ip_cache = _STUN_HOST
    return f"stun:{_stun_ip_cache}:{_STUN_PORT}"


class WebRTCAudioReceiver:
    """
    Manages a single active RTCPeerConnection for TX audio.
    Received PCM frames (mono, 48kHz, int16) are passed through a callback.
    """

    def __init__(self, on_pcm_frame, on_track_started=None, on_track_ended=None):
        """
        on_pcm_frame(pcm_bytes: bytes) — called for every PCM frame
        on_track_started() / on_track_ended() — optional hooks (e.g. start_tx/stop_tx)
        """
        self._pc: RTCPeerConnection | None = None
        self._on_pcm = on_pcm_frame
        self._on_start = on_track_started
        self._on_end   = on_track_ended
        self._task: asyncio.Task | None = None
        self.active = False

    async def handle_offer(self, sdp: str, type_: str = "offer") -> dict:
        """
        Accept an SDP offer from the client, return an SDP answer.
        If a previous connection exists — close it (one sender at a time).
        """
        await self.close()

        self._frame_count = 0
        if hasattr(self, "_resampler"):
            del self._resampler

        t0 = time.monotonic()
        config = RTCConfiguration(iceServers=[
            RTCIceServer(urls=await _stun_url())
        ])
        pc = RTCPeerConnection(configuration=config)
        self._pc = pc

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            print(f"[webrtc] Audio track received: {track.kind}")
            self.active = True
            if self._on_start:
                self._on_start()
            self._task = asyncio.ensure_future(self._consume_track(track))

        @pc.on("connectionstatechange")
        async def on_state_change():
            print(f"[webrtc] Connection state: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self.close()

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            print(f"[webrtc] ICE state: {pc.iceConnectionState}")

        @pc.on("icegatheringstatechange")
        async def on_ice_gathering_change():
            print(f"[webrtc] ICE gathering: {pc.iceGatheringState}")

        offer = RTCSessionDescription(sdp=sdp, type=type_)
        await pc.setRemoteDescription(offer)
        t1 = time.monotonic()

        offer_candidates = sdp.count('a=candidate')
        print(f"[webrtc] Offer from client: {offer_candidates} ICE candidates")

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        t2 = time.monotonic()

        # Wait for the server's ICE gathering to finish (max 3s)
        for _ in range(30):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)
        t3 = time.monotonic()

        final_sdp = pc.localDescription.sdp
        answer_candidates = final_sdp.count('a=candidate')
        print(f"[webrtc] Server answer: {answer_candidates} ICE candidates, gathering={pc.iceGatheringState} | "
              f"timing: setRemoteDescription={1000*(t1-t0):.0f}ms createAnswer/setLocal={1000*(t2-t1):.0f}ms "
              f"iceWait={1000*(t3-t2):.0f}ms total={1000*(t3-t0):.0f}ms")

        return {
            "sdp": final_sdp,
            "type": pc.localDescription.type,
        }

    async def add_ice_candidate(self, candidate: dict):
        """Add an ICE candidate from the client (trickle ICE)."""
        if not self._pc:
            return
        raw = candidate.get("candidate", "")
        if not raw:
            return  # empty candidate = end-of-candidates, ignore
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
            print(f"[webrtc] ICE candidate added: {ice_cand.host}:{ice_cand.port} type={ice_cand.type}")
        except Exception as e:
            print(f"[webrtc] ICE candidate error: {e} | raw={raw[:80]}")

    async def _consume_track(self, track):
        """Loop receiving audio frames and passing PCM to the callback."""
        try:
            while True:
                frame = await track.recv()
                # frame is an av.AudioFrame — convert to PCM int16 mono 48kHz
                pcm = self._frame_to_pcm(frame)
                if pcm and self._on_pcm:
                    self._on_pcm(pcm)
        except Exception as e:
            print(f"[webrtc] Track ended: {e}")
        finally:
            self.active = False
            if self._on_end:
                self._on_end()

    def _frame_to_pcm(self, frame) -> bytes:
        """Convert an av.AudioFrame -> PCM int16 mono @ 48kHz."""
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

            # Different sample rate - resampling needed (rare case)
            from av import AudioResampler
            if not hasattr(self, "_resampler"):
                self._resampler = AudioResampler(format="s16", layout="mono", rate=OPUS_RATE)
            frames = self._resampler.resample(frame)
            pcm = b""
            for f in frames:
                pcm += bytes(f.planes[0])
            return pcm

        except Exception as e:
            print(f"[webrtc] frame_to_pcm error: {e}")
            return b""

    async def close(self):
        """Close the active connection."""
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
