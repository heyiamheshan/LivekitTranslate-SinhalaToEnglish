"""Entry point for the gemini-bridge worker."""

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentServer, AutoSubscribe, JobContext
import logging, os

from bridge import run_bridge

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../../.env.local"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

server = AgentServer()


@server.rtc_session(agent_name="gemini-bridge")
async def bridge_session(ctx: JobContext):
    en_room_name = ctx.job.metadata
    if not en_room_name:
        log.error("gemini-bridge dispatched without Room B name in metadata — aborting")
        return
    log.info("Bridge starting: Room A=%s  Room B=%s", ctx.room.name, en_room_name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    await run_bridge(ctx.room, en_room_name)


if __name__ == "__main__":
    agents.cli.run_app(server)
