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
from uagents_core.contrib.protocols.payment import (
    CancelPayment, CommitPayment, CompletePayment, Funds, RejectPayment,
    RequestPayment, payment_protocol_spec,
)

load_dotenv(find_dotenv(usecwd=True))
from backend import BackendError, call  # noqa: E402

asyncio.set_event_loop(asyncio.new_event_loop())

agent = Agent(
    name="InterviewPilot",
    seed=os.environ["AGENT_SEED"],
    port=int(os.environ.get("AGENT_PORT", "8001")),
    mailbox=not os.environ.get("AGENT_ENDPOINT"),
    endpoint=[os.environ["AGENT_ENDPOINT"]] if os.environ.get("AGENT_ENDPOINT") else None,
    publish_agent_details=True,
)
chat = Protocol(spec=chat_protocol_spec)
payments = Protocol(spec=payment_protocol_spec, role="seller")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://fetchathon-backend.onrender.com").rstrip("/")
MIN_JD_CHARS = 80
PREP_PACK_PRICE = os.environ.get("PREP_PACK_PRICE_FET", "0.5")
PAYMENT_DEADLINE_SECONDS = 600

PREP_PACK_OFFER = (
    f"\n\n---\n💎 **Want the Prep Pack?** Model STAR answers for every weak response, the 8 questions you're "
    f"most likely to face next, a day-by-day 7-day plan and smart questions to ask: **{PREP_PACK_PRICE} FET**. "
    "Reply **prep pack** to unlock it."
)

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
    watch = ctx.storage.get("watch") or {}
    watch[sender] = room["sid"]
    ctx.storage.set("watch", watch)
    link = f"{PUBLIC_URL}/room/{room['sid']}"
    return (
        "🎙️ **Interview started.** Priya has read the job description.\n\n"
        f"🎥 **Face-to-face (voice + video):** {link}\n"
        "💬 Or simply reply here. Tell Priya whenever you want to wrap up and you'll get your scorecard.\n\n"
        f"---\n**Priya:** {room['opening']}"
    )


async def request_prep_pack(ctx: Context, sender: str, state: dict) -> str:
    if not state.get("sid"):
        return "Finish an interview first. The Prep Pack is built from your answers. " + HELP
    if state.get("active"):
        return "Your interview is still running. Wrap it up with Priya first, then reply **prep pack**."
    reference = f"prep-{state['sid']}"
    order = ctx.storage.get(f"pay:{reference}") or {}
    if order.get("paid"):
        pack = await call("prep_pack", sid=state["sid"])
        return f"💎 **Your Prep Pack**\n\n{pack}"
    ctx.storage.set(f"pay:{reference}", {"sender": sender, "sid": state["sid"]})
    await ctx.send(sender, RequestPayment(
        accepted_funds=[Funds(amount=PREP_PACK_PRICE, currency="FET", payment_method="fet_direct")],
        recipient=str(agent.wallet.address()),
        deadline_seconds=PAYMENT_DEADLINE_SECONDS,
        reference=reference,
        description="InterviewPilot Prep Pack: model answers, likely next questions and a 7-day plan",
    ))
    return f"💳 Payment request sent: **{PREP_PACK_PRICE} FET**. Approve it and your Prep Pack arrives right here."


async def handle_text(ctx: Context, sender: str, text: str) -> str:
    state = ctx.storage.get(sender) or {}
    low = text.strip().lower()

    if low == "result":
        if not state.get("sid"):
            return "No interview yet. " + HELP
        card = await call("room_result", sid=state["sid"])
        return f"📋 **Your scorecard**\n\n{card}{PREP_PACK_OFFER}" if card else "Your interview is still running. Tell Priya you'd like to wrap up to get your scorecard."

    if low in {"prep pack", "prep-pack", "preppack", "buy prep pack", "unlock prep pack"}:
        return await request_prep_pack(ctx, sender, state)

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
        return f"**Priya:** {turn['say']}\n\n🏁 **Interview complete.**\n\n📋 **Your scorecard**\n\n{card}{PREP_PACK_OFFER}"

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
            state = ctx.storage.get(sender) or {}
            if not state.get("active") and is_jd(item.text.strip().removeprefix("interview")):
                await ctx.send(sender, reply("⏳ Priya is reading the job description and preparing your interview…"))
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


def _unwatch(ctx: Context, sender: str) -> None:
    watch = ctx.storage.get("watch") or {}
    if watch.pop(sender, None) is not None:
        ctx.storage.set("watch", watch)


@agent.on_interval(period=20.0)
async def deliver_room_scorecards(ctx: Context):
    watch = ctx.storage.get("watch") or {}
    for sender, sid in list(watch.items()):
        state = ctx.storage.get(sender) or {}
        if state.get("sid") != sid or not state.get("active"):
            _unwatch(ctx, sender)
            continue
        try:
            card = await call("room_result", sid=sid)
        except BackendError as e:
            if e.status == 404:
                _unwatch(ctx, sender)
            continue
        if card:
            ctx.storage.set(sender, {**state, "active": False})
            _unwatch(ctx, sender)
            await ctx.send(sender, reply(f"🏁 **Your face-to-face interview is complete.**\n\n📋 **Your scorecard**\n\n{card}{PREP_PACK_OFFER}"))


@payments.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    order = ctx.storage.get(f"pay:{msg.reference}") if msg.reference else None
    if not order or order["sender"] != sender:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="Unknown or expired order."))
        return
    if order.get("paid"):
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        return
    ctx.storage.set(f"pay:{msg.reference}", {**order, "paid": True, "tx": msg.transaction_id})
    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    try:
        pack = await call("prep_pack", sid=order["sid"])
        text = f"✅ Payment received. Thank you!\n\n💎 **Your Prep Pack**\n\n{pack}"
    except BackendError as e:
        ctx.logger.error(str(e))
        text = "✅ Payment received. Your Prep Pack is taking longer than usual. Reply **prep pack** in a moment to receive it."
        ctx.storage.set(f"pay:{msg.reference}", {**order, "paid": True, "tx": msg.transaction_id, "delivered": False})
    await ctx.send(sender, reply(text))


@payments.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    await ctx.send(sender, reply("No problem, the Prep Pack is still available anytime. Reply **prep pack** to unlock it."))


agent.include(chat, publish_manifest=True)
agent.include(payments, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
