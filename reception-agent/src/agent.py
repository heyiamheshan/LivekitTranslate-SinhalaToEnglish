"""
Reception Agent — answers inbound SIP call from Sinhala speaker,
creates a separate room for the English caller, dials them in,
then dispatches the Gemini Bridge to translate bidirectionally
across the two rooms. Callers are in separate rooms so they never
hear each other's raw audio — only the translated output.
"""

from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import (
    AgentServer, AgentSession, Agent,
    JobContext, inference, TurnHandlingOptions,
)
import logging, os

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env.local"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

server = AgentServer()

ENGLISH_CALLER_IDENTITY = "english-speaker"


class ReceptionAgent(Agent):
    def __init__(self):
        super().__init__(
            instructions="""
            You are a reception agent answering an inbound phone call.
            This line is for Sinhala-speaking callers only.
            Say exactly this once using Sinhala phonetics, then stay silent:
            "Aayubowan! Karunaakara rændi sitinna, mama obawa sambandha karami."
            Do not say anything else.
            """
        )


@server.rtc_session(agent_name="reception-agent")
async def reception_session(ctx: JobContext):
    log.info("Inbound call in room: %s", ctx.room.name)

    session = AgentSession(
        stt=inference.STT("deepgram/nova-3"),
        llm=inference.LLM("openai/chat-latest"),
        tts=inference.TTS("cartesia/sonic-3"),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
        ),
    )
    await session.start(room=ctx.room, agent=ReceptionAgent())

    await session.say(
        "Aayubowan! Karunaakara rændi sitinna, mama obawa sambandha karami.",
        allow_interruptions=False,
    )

    target_phone = os.getenv("TARGET_PHONE_NUMBER")
    sip_trunk_id = os.getenv("SIP_TRUNK_ID", "ST_JXw7kviFNjBw")
    sip_address  = os.getenv("SIP_ADDRESS", "")

    if not target_phone:
        log.error("TARGET_PHONE_NUMBER not set in .env.local")
        await session.say("Kanagaatuyi, doshayak æthi viya.", allow_interruptions=False)
        return

    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    try:
        # Tag the inbound Sinhala caller
        participants = await lk.room.list_participants(
            api.ListParticipantsRequest(room=ctx.room.name)
        )
        for p in participants.participants:
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

        # Create a separate room for the English caller so they cannot
        # hear the Sinhala caller's raw audio and vice versa
        en_room_name = f"{ctx.room.name}-en"
        await lk.room.create_room(api.CreateRoomRequest(name=en_room_name))
        log.info("Created English caller room: %s", en_room_name)

        # Dial the English speaker into Room B — blocks until answered
        log.info("Dialling %s via trunk %s into %s", target_phone, sip_trunk_id, en_room_name)
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
        log.info("English speaker answered — in room %s", en_room_name)

        # Dispatch the bridge agent into Room A; it will connect to Room B
        # via metadata and handle all bidirectional translation
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="gemini-bridge",
                room=ctx.room.name,
                metadata=en_room_name,
            )
        )
        log.info("Gemini bridge dispatched into %s (Room B: %s)", ctx.room.name, en_room_name)

    except Exception as e:
        log.error("Transfer failed: %s", e, exc_info=True)
        await session.say(
            "Kanagaatuyi, sambandhathaawaya sthaapitha kireemata nohæki viya. "
            "Karunaakara næwath amatanna.",
            allow_interruptions=False,
        )
    finally:
        await lk.aclose()


if __name__ == "__main__":
    agents.cli.run_app(server)
