"""
Gemini Bridge — cross-room bidirectional translation.

Dispatched into Room A (Sinhala caller's room).
Room B name (English caller's room) comes from job metadata.

Each caller is in their own room so they never hear each other's raw audio.
The bridge translates:
  Room A audio (si) → Gemini → English → published into Room B
  Room B audio (en) → Gemini → Sinhala → published into Room A
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import os

import websockets
from livekit import api, rtc

log = logging.getLogger(__name__)

GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_INPUT_SAMPLE_RATE = 16000
GEMINI_OUTPUT_SAMPLE_RATE = 24000
_DELAYS = [1, 2, 5, 10, 30]


class CrossRoomTranslator:
    """
    Reads audio from `speaker_identity` in `input_room`,
    translates to `target_lang` via Gemini Live,
    and publishes the translated audio into `output_room`.
    """

    def __init__(
        self,
        *,
        input_room: rtc.Room,
        output_room: rtc.Room,
        speaker_identity: str,
        target_lang: str,
        gemini_api_key: str,
    ) -> None:
        self._input_room = input_room
        self._output_room = output_room
        self._speaker_identity = speaker_identity
        self._target_lang = target_lang
        self._api_key = gemini_api_key
        self._source = rtc.AudioSource(GEMINI_OUTPUT_SAMPLE_RATE, 1)
        self._local_track: rtc.LocalAudioTrack | None = None
        self._track_sid: str | None = None
        self._task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._failures = 0

    async def start(self) -> None:
        name = f"tx:{self._speaker_identity}:{self._target_lang}"
        self._local_track = rtc.LocalAudioTrack.create_audio_track(name, self._source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        pub = await self._output_room.local_participant.publish_track(self._local_track, opts)
        self._track_sid = pub.sid
        log.info("Bridge track %s published (sid=%s)", name, self._track_sid)
        self._task = asyncio.create_task(self._run(), name=f"bridge/{name}")

    async def aclose(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._track_sid:
            try:
                await self._output_room.local_participant.unpublish_track(self._track_sid)
            except Exception:
                pass
        try:
            await self._source.aclose()
        except Exception:
            pass

    def _find_track(self) -> rtc.RemoteAudioTrack | None:
        p = self._input_room.remote_participants.get(self._speaker_identity)
        if p is None:
            return None
        for pub in p.track_publications.values():
            if pub.track and isinstance(pub.track, rtc.RemoteAudioTrack):
                return pub.track
        return None

    async def _run(self) -> None:
        while not self._closed.is_set():
            track = self._find_track()
            if not track:
                await asyncio.sleep(1)
                continue
            try:
                await self._pump(track)
                return  # speaker left cleanly
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures += 1
                delay = _DELAYS[min(self._failures - 1, len(_DELAYS) - 1)]
                log.warning(
                    "Bridge error %s→%s attempt #%d: %s; retrying in %.1fs",
                    self._speaker_identity, self._target_lang, self._failures, exc, delay,
                )
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _pump(self, speaker_track: rtc.RemoteAudioTrack) -> None:
        model = os.getenv("GEMINI_MODEL", "gemini-3.5-live-translate-preview")
        url = f"{GEMINI_WS_URL}?key={self._api_key}"
        async with websockets.connect(url, max_size=2**22, ping_interval=60, ping_timeout=30) as ws:
            await ws.send(json.dumps({
                "setup": {
                    "model": f"models/{model}",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "translationConfig": {
                            "targetLanguageCode": self._target_lang,
                            "echoTargetLanguage": True,
                        },
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Aoede"}
                            }
                        },
                    },
                    "realtimeInputConfig": {
                        "automaticActivityDetection": {"disabled": True},
                    },
                }
            }))
            log.info("Gemini WS open: %s→%s", self._speaker_identity, self._target_lang)
            ready = asyncio.Event()
            send_t = asyncio.create_task(self._send(ws, ready, speaker_track))
            recv_t = asyncio.create_task(self._recv(ws, ready))
            done, pending = await asyncio.wait({send_t, recv_t}, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc

    async def _send(
        self,
        ws: websockets.WebSocketClientProtocol,
        ready: asyncio.Event,
        track: rtc.RemoteAudioTrack,
    ) -> None:
        await ready.wait()
        stream = rtc.AudioStream(track, sample_rate=GEMINI_INPUT_SAMPLE_RATE, num_channels=1)
        # batch ~100ms of audio before sending to reduce WebSocket overhead
        BATCH_SAMPLES = GEMINI_INPUT_SAMPLE_RATE // 10  # 1600 samples = 100ms
        mime = f"audio/pcm;rate={GEMINI_INPUT_SAMPLE_RATE}"
        accumulated = bytearray()
        sent = 0
        try:
            async for ev in stream:
                if self._closed.is_set():
                    return
                accumulated.extend(bytes(ev.frame.data))
                if len(accumulated) >= BATCH_SAMPLES * 2:  # *2 for 16-bit samples
                    b64 = base64.b64encode(bytes(accumulated)).decode("ascii")
                    await ws.send(json.dumps({
                        "realtimeInput": {"audio": {"mimeType": mime, "data": b64}}
                    }))
                    accumulated.clear()
                    sent += 1
                    if sent in (1, 10) or sent % 100 == 0:
                        log.info(
                            "Bridge →Gemini: %d batches %s→%s",
                            sent, self._speaker_identity, self._target_lang,
                        )
        finally:
            # flush any remaining audio
            if accumulated and not self._closed.is_set():
                b64 = base64.b64encode(bytes(accumulated)).decode("ascii")
                await ws.send(json.dumps({
                    "realtimeInput": {"audio": {"mimeType": mime, "data": b64}}
                }))
            await stream.aclose()

    async def _recv(
        self,
        ws: websockets.WebSocketClientProtocol,
        ready: asyncio.Event,
    ) -> None:
        async for raw in ws:
            if self._closed.is_set():
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("setupComplete") is not None:
                self._failures = 0
                ready.set()
                continue

            sc = msg.get("serverContent")
            if not sc:
                continue

            mt = sc.get("modelTurn")
            if mt:
                for part in (mt.get("parts") or []):
                    inline = part.get("inlineData")
                    if inline and inline.get("data"):
                        pcm = base64.b64decode(inline["data"])
                        samples = array.array("h")
                        samples.frombytes(pcm)
                        frame = rtc.AudioFrame(
                            data=samples.tobytes(),
                            sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
                            num_channels=1,
                            samples_per_channel=len(samples),
                        )
                        try:
                            await self._source.capture_frame(frame)
                        except Exception as exc:
                            if "closed" not in str(exc).lower():
                                raise


async def run_bridge(si_room: rtc.Room, en_room_name: str) -> None:
    """
    Connect to Room B (English caller's room) and run bidirectional
    cross-room translation until both callers disconnect.
    """
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    lk_url = os.getenv("LIVEKIT_URL", "")
    api_key_val = os.getenv("LIVEKIT_API_KEY", "")
    api_secret_val = os.getenv("LIVEKIT_API_SECRET", "")

    # Connect to Room B as a regular participant
    token = (
        api.AccessToken(api_key=api_key_val, api_secret=api_secret_val)
        .with_identity("gemini-bridge-en")
        .with_name("Gemini Bridge")
        .with_grants(api.VideoGrants(
            room_join=True,
            room=en_room_name,
            can_publish=True,
            can_subscribe=True,
        ))
        .to_jwt()
    )
    en_room = rtc.Room()
    await en_room.connect(lk_url, token)
    log.info("Bridge joined Room B: %s", en_room_name)

    # Wait for SIP participants to appear in both rooms (event-driven)
    si_identity: str | None = None
    en_identity: str | None = None
    both_found = asyncio.Event()

    for p in si_room.remote_participants.values():
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            si_identity = p.identity
            break
    for p in en_room.remote_participants.values():
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            en_identity = p.identity
            break

    if si_identity and en_identity:
        both_found.set()

    @si_room.on("participant_connected")
    def _si_joined(participant: rtc.RemoteParticipant) -> None:
        nonlocal si_identity
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            si_identity = participant.identity
            if en_identity:
                both_found.set()

    @en_room.on("participant_connected")
    def _en_joined(participant: rtc.RemoteParticipant) -> None:
        nonlocal en_identity
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            en_identity = participant.identity
            if si_identity:
                both_found.set()

    try:
        await asyncio.wait_for(both_found.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        log.error(
            "SIP participants not found within 15s — si=%s en=%s; aborting bridge",
            si_identity, en_identity,
        )
        await en_room.disconnect()
        return

    log.info(
        "Bridge: si_caller=%s (Room A: %s), en_caller=%s (Room B: %s)",
        si_identity, si_room.name, en_identity, en_room_name,
    )

    si_to_en = CrossRoomTranslator(
        input_room=si_room,
        output_room=en_room,
        speaker_identity=si_identity,
        target_lang="en",
        gemini_api_key=gemini_key,
    )
    en_to_si = CrossRoomTranslator(
        input_room=en_room,
        output_room=si_room,
        speaker_identity=en_identity,
        target_lang="si",
        gemini_api_key=gemini_key,
    )

    await si_to_en.start()
    await en_to_si.start()
    log.info("Bridge bidirectional translation active")

    # Event-driven call linking: when either party hangs up, end both calls.
    call_ended = asyncio.Event()

    @si_room.on("participant_disconnected")
    def _si_hung_up(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == si_identity:
            log.info("Sinhala caller (%s) hung up", si_identity)
            call_ended.set()

    @en_room.on("participant_disconnected")
    def _en_hung_up(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == en_identity:
            log.info("English caller (%s) hung up", en_identity)
            call_ended.set()

    lk = api.LiveKitAPI(
        url=lk_url,
        api_key=api_key_val,
        api_secret=api_secret_val,
    )

    try:
        await call_ended.wait()

        # Hang up whichever caller is still on the line
        if si_identity in si_room.remote_participants:
            log.info("Hanging up Sinhala caller %s", si_identity)
            await lk.room.remove_participant(
                api.RemoveParticipantRequest(room=si_room.name, identity=si_identity)
            )
        if en_identity in en_room.remote_participants:
            log.info("Hanging up English caller %s", en_identity)
            await lk.room.remove_participant(
                api.RemoveParticipantRequest(room=en_room_name, identity=en_identity)
            )

    finally:
        await lk.aclose()
        await si_to_en.aclose()
        await en_to_si.aclose()
        await en_room.disconnect()
        log.info("Bridge closed")
