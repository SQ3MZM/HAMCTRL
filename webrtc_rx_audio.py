#!/usr/bin/env python3
"""
webrtc_rx_audio.py — sends RX audio (radio -> browser) over WebRTC (aiortc).
The RX audio path (2026-08-24 onward), replacing the direct
browser<->ham_audio.exe WebSocket/Opus RX path. Reported live over LTE: RX
audio and control both degraded together under any competing network
traffic, worst on WS - classic TCP head-of-line blocking, where one
delayed/lost segment stalls everything queued behind it on the same
connection, regardless of how unrelated that data is. TX mic audio already
went over WebRTC (webrtc_audio.py, aiortc/UDP) and didn't have this problem;
this brings RX to the same transport so a lost packet is a small glitch
instead of a multi-second stall - live-confirmed clearly smoother with 1
listener than the old WS path.

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
listening isn't exclusive like TX) - one instance per connected client,
tracked in App._webrtc_rx_senders keyed by the ws. Each listener is a
separate aiortc connection/track (independent Opus encode per listener,
unlike ham_audio.exe's Rust path which encodes once and fans out the same
bytes to every WS client) - see App._log_rx_webrtc_listeners for the
listener-count/CPU log line used to measure how this scales in practice.
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
        self._recv_count = 0

    async def recv(self):
        # DIAGNOSTIC (2026-08-24): reported live - the connection reaches
        # ICE 'completed'/state 'connected', real audio is flowing
        # server-side (_rx_loop's own frame counter proves that, but NOT
        # that THIS track's queue is actually being drained), then closes
        # on its own after ~20-30s with no 'failed'/'disconnected' state in
        # between - meaning something OTHER than the connectionstatechange
        # handler is closing it. If recv() itself raises, aiortc's RTP
        # send loop swallows it silently and tears the connection down -
        # this print is the only way to see that instead of guessing.
        #
        # pts: sample-count based, NOT wall-clock. A wall-clock version was
        # tried (2026-08-24) on the theory that _safe_put_nowait
        # (audio_stream.py) dropping chunks under load was desyncing a
        # naive running counter - it turned out NOT to be the cause of the
        # real bug that day (a client-side createMediaStreamSource issue,
        # fixed in ws.js) and wall-clock pts on its own measurably made
        # playback choppier: real recv() scheduling jitter (asyncio/OS,
        # a few ms here and there) leaked directly into frame spacing,
        # where Opus/RTP expect each chunk to represent an exact, uniform
        # duration. Sample-count keeps that duration exact regardless of
        # when recv() actually got scheduled - a genuinely dropped chunk
        # just becomes a small silent gap, not a source of constant jitter.
        try:
            import av
            pcm = await self._queue.get()
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
            frame.planes[0].update(pcm)
            frame.sample_rate = self._rate
            frame.pts = self._timestamp
            frame.time_base = fractions.Fraction(1, self._rate)
            self._timestamp += len(pcm) // 2
            self._recv_count += 1
            if self._recv_count % 250 == 0:
                print(f"[webrtc-rx] track recv() #{self._recv_count} OK", flush=True)
            return frame
        except Exception as e:
            import traceback
            print(f"[webrtc-rx] track recv() FAILED after {self._recv_count} frames: {e}", flush=True)
            traceback.print_exc()
            raise

    def stop(self):
        print(f"[webrtc-rx] track stop() called after {self._recv_count} recv() calls", flush=True)
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
            # 'disconnected' is DELIBERATELY not included here - per the
            # WebRTC spec it's a transient state (the ICE transport is
            # actively trying to recover, e.g. after a brief real-world
            # network hiccup) that often self-heals within a second or two
            # without any action needed. Treating it the same as 'failed'
            # (a decisive, unrecoverable ICE failure) tore down and forced
            # a full renegotiation on every minor blip - reported live as
            # "connects, then dies almost immediately, repeatedly" over a
            # real internet path (DuckDNS tunnel, not localhost/LAN).
            if pc.connectionState == "failed":
                await self.close()

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            # Separate from connectionstatechange above - ICE state alone
            # narrows down whether a future "failed" is a real NAT/STUN
            # traversal problem (ICE itself never reaches "completed") vs.
            # something else entirely (ICE fine, DTLS or media path issue).
            print(f"[webrtc-rx] ICE state: {pc.iceConnectionState}")

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
        # DIAGNOSTIC (2026-08-24): logs WHO called close() (a short stack
        # summary) - see the matching comment on _RxAudioTrack.recv(). If
        # this fires from somewhere OTHER than "webrtc_rx_start" (a fresh
        # connection replacing an old one) or "failed" in
        # on_state_change, that's direct evidence of a third, not-yet-
        # found trigger closing otherwise-healthy connections.
        if self._pc:
            import traceback
            caller = traceback.extract_stack()[-2]
            print(f"[webrtc-rx] close() called from {caller.name} ({caller.filename}:{caller.lineno})", flush=True)
        if self._track:
            self._track.stop()
            self._track = None
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None
