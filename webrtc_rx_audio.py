#!/usr/bin/env python3
"""
webrtc_rx_audio.py — sends RX audio (radio -> browser) over WebRTC (aiortc).
TEST BUILD for internal use: an alternative to the existing Rust/ham_audio.exe
-> browser WebSocket RX path, not a replacement for it (yet). Reported live
over LTE: RX audio and control both degrade together under any competing
network traffic, worst on WS - classic TCP head-of-line blocking, where one
delayed/lost segment stalls everything queued behind it on the same
connection, regardless of how unrelated that data is. TX mic audio already
goes over WebRTC (webrtc_audio.py, aiortc/UDP) and doesn't have this problem;
this brings RX to the same transport so a lost packet is a small glitch
instead of a multi-second stall.

Audio source: the ALREADY-RUNNING AudioStream._rx_loop PCM capture
(audio_stream.py) that normally feeds only the CW decoder (DeepCW) and the
local waterfall preview - subscribe_rx_pcm() taps the same physical capture
instead of opening a second one.

Direction is the reverse of webrtc_audio.py: here the SERVER has the media
to send, so the SERVER creates the offer (browser answers) - opposite of TX,
where the browser (with the mic) offers and the server answers.

Signaling goes over the existing WebSocket (/ws), message types
webrtc_rx_start/webrtc_rx_answer/webrtc_rx_ice/webrtc_rx_stop - deliberately
separate names from webrtc_offer/answer/ice/stop (TX) to avoid any ambiguity
about which direction a given message is for.

Multiple clients can each have their own WebRTCAudioSender at once (RX
listening isn't exclusive like TX) - one instance per connected client that
opts into the test build, tracked in App._webrtc_rx_senders keyed by the ws.
"""
import asyncio, fractions
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.mediastreams import MediaStreamTrack
from webrtc_audio import _stun_url, add_ice_candidate_to_pc

OPUS_RATE = 48000


class _RxAudioTrack(MediaStreamTrack):
    """Pulls PCM chunks from an AudioStream subscriber queue and yields them
    as av.AudioFrame - aiortc encodes to Opus/RTP internally. Paced by the
    real hardware capture rate on the OTHER end of the queue (each chunk is
    ~20ms of real audio, produced roughly every 20ms by _rx_loop's blocking
    PyAudio read) - no manual sleep/clock-tracking needed here, awaiting the
    queue already blocks for the right amount of time."""
    kind = "audio"

    def __init__(self, audio_stream, loop):
        super().__init__()
        self._audio_stream = audio_stream
        self._queue = audio_stream.subscribe_rx_pcm(loop)
        self._rate = getattr(audio_stream, "_rx_rate", None) or OPUS_RATE
        self._timestamp = 0

    async def recv(self):
        import av
        pcm = await self._queue.get()
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
        frame.planes[0].update(pcm)
        frame.sample_rate = self._rate
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, self._rate)
        self._timestamp += len(pcm) // 2
        return frame

    def stop(self):
        self._audio_stream.unsubscribe_rx_pcm(self._queue)
        super().stop()


class WebRTCAudioSender:
    """One instance per client that opts into WebRTC RX audio. Server-side
    offer/answer flow (mirrors WebRTCAudioReceiver's structure, opposite
    roles)."""

    def __init__(self, audio_stream):
        self._audio_stream = audio_stream
        self._pc: RTCPeerConnection | None = None
        self._track: _RxAudioTrack | None = None

    async def create_offer(self) -> dict:
        """Start a fresh connection (closing any previous one for this
        client) and return an SDP offer for the browser to answer."""
        await self.close()

        config = RTCConfiguration(iceServers=[RTCIceServer(urls=await _stun_url())])
        pc = RTCPeerConnection(configuration=config)
        self._pc = pc

        loop = asyncio.get_running_loop()
        track = _RxAudioTrack(self._audio_stream, loop)
        self._track = track
        pc.addTrack(track)

        @pc.on("connectionstatechange")
        async def on_state_change():
            print(f"[webrtc-rx] Connection state: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self.close()

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for ICE gathering to finish (max 3s) so the offer carries all
        # candidates - same non-trickle-on-the-way-out approach as the TX
        # answer path in webrtc_audio.py, kept for consistency/simplicity.
        for _ in range(30):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    async def set_answer(self, sdp: str, type_: str = "answer"):
        if not self._pc:
            return
        answer = RTCSessionDescription(sdp=sdp, type=type_)
        await self._pc.setRemoteDescription(answer)

    async def add_ice_candidate(self, candidate: dict):
        if not self._pc:
            return
        await add_ice_candidate_to_pc(self._pc, candidate)

    async def close(self):
        if self._track:
            self._track.stop()
            self._track = None
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None
