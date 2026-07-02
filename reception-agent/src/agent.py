"""
Reception Agent — answers inbound SIP call from Sinhala speaker,
plays hold music while dialling the English caller into a separate room,
stops music when the English caller answers, then dispatches the Gemini Bridge.
"""

from __future__ import annotations

import array
import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env.local"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

server = AgentServer()
ENGLISH_CALLER_IDENTITY = "english-speaker"
MUSIC_SAMPLE_RATE = 48000
MUSIC_CHUNK_MS = 100  # ms per audio frame


async def _decode_mp3(path: str, sample_rate: int) -> bytes:
    """Decode MP3 to raw s16le mono PCM in a thread (non-blocking)."""
    def _decode() -> bytes:
        import av as _av
        container = _av.open(path)
        resampler = _av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        chunks: list[bytes] = []
        for frame in container.decode(audio=0):
            for r in resampler.resample(frame):
                chunks.append(bytes(r.planes[0]))
        for r in resampler.resample(None):  # flush resampler
            chunks.append(bytes(r.planes[0]))
        container.close()
        return b"".join(chunks)

    return await asyncio.get_event_loop().run_in_executor(None, _decode)


async def play_mp3_to_room(
    room: rtc.Room,
    mp3_path: str,
    stop_event: asyncio.Event,
) -> None:
    """Publish an MP3 on loop into the room until stop_event is set."""
    if not os.path.exists(mp3_path):
        log.warning("Hold music not found at %s — waiting silently", mp3_path)
        await stop_event.wait()
        return

    source = rtc.AudioSource(MUSIC_SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("hold-music", source)
    opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    pub = await room.local_participant.publish_track(track, opts)
    log.info("Hold music started")

    bytes_per_chunk = MUSIC_SAMPLE_RATE * 2 * MUSIC_CHUNK_MS // 1000  # s16 = 2 bytes/sample

    try:
        pcm = await _decode_mp3(mp3_path, MUSIC_SAMPLE_RATE)

        while not stop_event.is_set():
            offset = 0
            while offset < len(pcm) and not stop_event.is_set():
                chunk = pcm[offset : offset + bytes_per_chunk]
                offset += bytes_per_chunk
                if len(chunk) < 2:
                    break
                if len(chunk) % 2:
                    chunk = chunk[:-1]  # keep even byte count for s16
                samples = array.array("h")
                samples.frombytes(chunk)
                await source.capture_frame(rtc.AudioFrame(
                    data=samples.tobytes(),
                    sample_rate=MUSIC_SAMPLE_RATE,
                    num_channels=1,
                    samples_per_channel=len(samples),
                ))
    finally:
        try:
            await room.local_participant.unpublish_track(pub.sid)
        except Exception:
            pass
        try:
            await source.aclose()
        except Exception:
            pass
        log.info("Hold music stopped")


@server.rtc_session(agent_name="reception-agent")
async def reception_session(ctx: JobContext):
    log.info("Inbound call in room: %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    target_phone = os.getenv("TARGET_PHONE_NUMBER")
    sip_trunk_id = os.getenv("SIP_TRUNK_ID", "ST_JXw7kviFNjBw")
    sip_address  = os.getenv("SIP_ADDRESS", "")

    if not target_phone:
        log.error("TARGET_PHONE_NUMBER not set")
        return

    music_stop = asyncio.Event()
    music_path = os.path.join(os.path.dirname(__file__), "Intercall - Hold Music.mp3")
    music_task = asyncio.create_task(
        play_mp3_to_room(ctx.room, music_path, music_stop)
    )

    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    try:
        # Tag the inbound Sinhala caller
        resp = await lk.room.list_participants(
            api.ListParticipantsRequest(room=ctx.room.name)
        )
        for p in resp.participants:
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                await lk.room.update_participant(
                    api.UpdateParticipantRequest(
                        room=ctx.room.name,
                        identity=p.identity,
                        attributes={"lang": "si"},
                    )
                )
                log.info("Set lang=si on inbound caller %s", p.identity)
                break

        # Create a separate room for the English caller
        en_room_name = f"{ctx.room.name}-en"
        await lk.room.create_room(api.CreateRoomRequest(name=en_room_name))
        log.info("Created English caller room: %s", en_room_name)

        # Dial English speaker — blocks until they answer
        log.info("Dialling %s via trunk %s", target_phone, sip_trunk_id)
        await lk.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=target_phone,
                sip_request_uri={"raw": f"sip:{target_phone}@{sip_address}"} if sip_address else None,
                room_name=en_room_name,
                participant_identity=ENGLISH_CALLER_IDENTITY,
                participant_name="English Speaker",
                participant_attributes={"lang": "en"},
                wait_until_answered=True,
            )
        )
        log.info("English speaker answered — stopping hold music")
        music_stop.set()

        # Dispatch the bridge for bidirectional translation
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="gemini-bridge",
                room=ctx.room.name,
                metadata=en_room_name,
            )
        )
        log.info("Gemini bridge dispatched (Room A: %s, Room B: %s)", ctx.room.name, en_room_name)

    except Exception as e:
        log.error("Transfer failed: %s", e, exc_info=True)
    finally:
        music_stop.set()
        await music_task
        await lk.aclose()


if __name__ == "__main__":
    agents.cli.run_app(server)
