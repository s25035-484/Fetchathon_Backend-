import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import find_dotenv, load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, StartSessionContent,
    TextContent, chat_protocol_spec,
)

load_dotenv(find_dotenv(usecwd=True))
from backend import BackendError, call  # noqa: E402

asyncio.set_event_loop(asyncio.new_event_loop())

agent = Agent(
    name="InterviewPilot",
    seed=os.environ["AGENT_SEED"],
    port=int(os.environ.get("AGENT_PORT", "8001")),
    mailbox=True,
    publish_agent_details=True,
)
chat = Protocol(spec=chat_protocol_spec)

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://fetchathon-backend.onrender.com").rstrip("/")
MIN_JD_CHARS = 80

HELP = (
    "👋 Hi, I'm **InterviewPilot**: a live AI HR interviewer.\n\n"
    "**Paste a job description** (or type `interview` followed by the JD) and Priya, our HR recruiter, "
    "will interview you for that exact role, as a natural back-and-forth conversation that adapts to your answers.\n\n"
    "• Answer right here in chat, **or** open the 🎥 face-to-face room link for a voice + video interview\n"
    "• Tell Priya whenever you want to wrap up; she decides when the interview is complete → scorecard against every JD requirement\n"
    "• Type **result** to fetch the scorecard of a video interview"
)


def reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(text=text)])


def is_jd(text: str) -> bool:
    return len(text.strip()) >= MIN_JD_CHARS


async def start(ctx: Context, sender: str, jd: str) -> str:
    room = await call("create_room", jd=jd)
    ctx.storage.set(sender, {"sid": room["sid"], "active": True})
    link = f"{PUBLIC_URL}/room/{room['sid']}"
    return (
        "🎙️ **Interview started.** Priya has read the job description.\n\n"
        f"🎥 **Face-to-face (voice + video):** {link}\n"
        "💬 Or simply reply here. Tell Priya whenever you want to wrap up and you'll get your scorecard.\n\n"
        f"---\n**Priya:** {room['opening']}"
    )


async def handle_text(ctx: Context, sender: str, text: str) -> str:
    state = ctx.storage.get(sender) or {}
    low = text.strip().lower()

    if low == "result":
        if not state.get("sid"):
            return "No interview yet. " + HELP
        card = await call("room_result", sid=state["sid"])
        return f"📋 **Your scorecard**\n\n{card}" if card else "Your interview is still running. Tell Priya you'd like to wrap up to get your scorecard."

    if low.startswith("interview"):
        jd = text.strip()[len("interview"):].strip(" :-\n")
        if not is_jd(jd):
            return "Please paste the full job description after `interview` (a few lines at least)."
        return await start(ctx, sender, jd)

    if state.get("active"):
        try:
            turn = await call("room_turn", sid=state["sid"], text=text)
        except BackendError as e:
            if e.status != 404:
                raise
            ctx.storage.set(sender, {})
            return await start(ctx, sender, text) if is_jd(text) else HELP
        if not turn["end"]:
            return f"**Priya:** {turn['say']}"
        card = await call("room_end", sid=state["sid"])
        ctx.storage.set(sender, {**state, "active": False})
        return f"**Priya:** {turn['say']}\n\n🏁 **Interview complete.**\n\n📋 **Your scorecard**\n\n{card}"

    if is_jd(text):
        return await start(ctx, sender, text)
    return HELP


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"session start {sender}")
        elif isinstance(item, TextContent):
            try:
                text = await handle_text(ctx, sender, item.text)
            except BackendError as e:
                ctx.logger.error(str(e))
                text = "⚠️ The interview service is busy right now. Please send your message again in a moment."
            except Exception:  # noqa: BLE001
                ctx.logger.exception("handler failed")
                text = "⚠️ Something went wrong on my side. Please try again."
            await ctx.send(sender, reply(text))
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"session end {sender}")


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
