import asyncio
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import find_dotenv, load_dotenv
import hmac

from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, StartSessionContent,
    TextContent, chat_protocol_spec,
)
from uagents_core.contrib.protocols.chat import MetadataContent
from uagents_core.contrib.protocols.chat.cards import (
    CarouselCardPayload, CarouselItem, CtaAction, DetailCardPayload, DetailSummaryRow,
    ReviewCardPayload, ReviewSummaryRow, create_card_content, extract_card_response,
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
    handle_messages_concurrently=True,
    description="Live AI HR interviewer: paste any job description and Priya interviews you for that exact role, "
                "then returns a hiring scorecard and an optional paid Prep Pack.",
    readme_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "AGENT_README.md"),
    metadata={"categories": ["career", "interview", "education", "hr"], "innovationlab": True, "hackathon": "fetchathon-3.0"},
)
chat = Protocol(spec=chat_protocol_spec)
payments = Protocol(spec=payment_protocol_spec, role="seller")

PUBLIC_URL = "https://fetchathon-backend.onrender.com"
MIN_JD_CHARS = 80
MENTION = re.compile(r"@agent1[0-9a-z]+", re.IGNORECASE)
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


def reply(text: str, card: DetailCardPayload | None = None) -> ChatMessage:
    content = [TextContent(text=text)]
    if card is not None:
        content.append(create_card_content(card, card_id=uuid4()))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


def interview_card() -> DetailCardPayload:
    return DetailCardPayload(
        title="Interview in progress",
        summary_rows=[DetailSummaryRow(label="Interviewer", value="Priya (ASI1)"),
                      DetailSummaryRow(label="Modes", value="Chat or face-to-face room")],
        ctas=[CtaAction(label="Wrap up & get scorecard", selection={"action": "wrap_up"}, primary=True)],
    )


def scorecard_card() -> DetailCardPayload:
    return DetailCardPayload(
        title="What next?",
        summary_rows=[DetailSummaryRow(label="Prep Pack", value=f"{PREP_PACK_PRICE} FET via Payment Protocol")],
        ctas=[CtaAction(label=f"Get Prep Pack ({PREP_PACK_PRICE} FET)", selection={"action": "prep_pack"}, primary=True),
              CtaAction(label="New interview", selection={"action": "new"})],
    )


SAMPLE_JDS = {
    "backend": ("Backend Engineer", "Backend Engineer (Python). 2+ years building REST APIs with Django or FastAPI, PostgreSQL, Redis caching, Docker and AWS. You will design scalable services, write tests and collaborate with product."),
    "frontend": ("Frontend Developer", "Frontend Developer (React). 1+ year with React, TypeScript, REST APIs, responsive UI and Git. You will build user-facing features, ensure accessibility and work closely with designers."),
    "analyst": ("Data Analyst", "Data Analyst. Strong SQL, Excel and Python (pandas), dashboards in Power BI or Tableau, basic statistics. You will turn business questions into analyses and present insights to stakeholders."),
}


def roles_card() -> CarouselCardPayload:
    return CarouselCardPayload(
        title="Quick start",
        subtitle="Pick a sample role, or paste your own job description",
        items=[CarouselItem(id=key, title=title, subtitle="Sample JD", secondary_text=jd[:90] + "…",
                            primary_cta=CtaAction(label="Start interview", selection={"action": "sample", "role": key}, primary=True))
               for key, (title, jd) in SAMPLE_JDS.items()],
    )


def checkout_card() -> ReviewCardPayload:
    return ReviewCardPayload(
        title="Confirm Prep Pack",
        summary_rows=[ReviewSummaryRow(label="Item", value="InterviewPilot Prep Pack"),
                      ReviewSummaryRow(label="Includes", value="STAR model answers, 8 likely questions, 7-day plan"),
                      ReviewSummaryRow(label="Price", value=f"{PREP_PACK_PRICE} FET (Payment Protocol)")],
        approve_cta=CtaAction(label=f"Pay {PREP_PACK_PRICE} FET", selection={"action": "confirm_pay"}, primary=True),
        reject_cta=CtaAction(label="Not now", selection={"action": "cancel_pay"}),
    )


CARD_ACTIONS = {"wrap_up": "I'd like to wrap up the interview now, please."}


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
    ), interview_card()


async def request_prep_pack(ctx: Context, sender: str, state: dict) -> str:
    if not state.get("sid"):
        return "Finish an interview first. The Prep Pack is built from your answers. " + HELP
    if state.get("active"):
        return "Your interview is still running. Wrap it up with Priya first, then reply **prep pack**."
    reference = f"prep-{state['sid']}"
    order = ctx.storage.get(f"pay:{reference}") or {}
    if order.get("paid"):
        await ctx.send(sender, reply("⏳ Preparing your Prep Pack…"))
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
        return f"📋 **Your scorecard**\n\n{card}{PREP_PACK_OFFER}", scorecard_card() if card else "Your interview is still running. Tell Priya you'd like to wrap up to get your scorecard."

    if low in {"prep pack", "prep-pack", "preppack", "buy prep pack", "unlock prep pack"}:
        if not state.get("sid") or state.get("active"):
            return await request_prep_pack(ctx, sender, state)
        return "Here's your Prep Pack summary. Confirm to pay through the Fetch.ai Payment Protocol.", checkout_card()

    if low.startswith("interview"):
        jd = text.strip()[len("interview"):].strip(" :-\n")
        if not is_jd(jd):
            return ("To run a realistic interview I need the **job description**. Please paste it (a few lines: role, "
                    "skills, responsibilities), e.g. *Backend Engineer: 2+ years Python, Django, PostgreSQL, AWS...*")
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
        await ctx.send(sender, reply(f"**Priya:** {turn['say']}\n\n🏁 **Interview complete.** ⏳ Writing your scorecard…"))
        card = await call("room_end", sid=state["sid"])
        ctx.storage.set(sender, {**state, "active": False})
        _unwatch(ctx, sender)
        return f"📋 **Your scorecard**\n\n{card}{PREP_PACK_OFFER}"

    if is_jd(text):
        return await start(ctx, sender, text)
    return HELP


async def handle_card_action(ctx: Context, sender: str, action: str, selection: dict) -> None:
    state = ctx.storage.get(sender) or {}
    try:
        if action == "sample" and selection.get("role") in SAMPLE_JDS:
            await ctx.send(sender, reply("⏳ Priya is reading the job description and preparing your interview…"))
            text, card = await start(ctx, sender, SAMPLE_JDS[selection["role"]][1])
        elif action == "prep_pack":
            text, card = "Here's your Prep Pack summary. Confirm to pay through the Fetch.ai Payment Protocol.", checkout_card()
        elif action == "confirm_pay":
            text, card = await request_prep_pack(ctx, sender, state), None
        elif action == "cancel_pay":
            text, card = "No problem. The Prep Pack stays available whenever you want it.", None
        else:
            return
    except BackendError as e:
        ctx.logger.error(str(e))
        text, card = "⚠️ The interview service is busy right now. Please try again in a moment.", None
    await ctx.send(sender, reply(text, card))


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    for item in msg.content:
        if isinstance(item, MetadataContent):
            response = extract_card_response(item)
            action = (response.selection or {}).get("action") if response and response.selection else None
            if action == "new":
                ctx.storage.set(sender, {})
                await ctx.send(sender, reply("Paste the next job description, or pick a sample role.", roles_card()))
                continue
            if action in {"sample", "prep_pack", "confirm_pay", "cancel_pay"}:
                await handle_card_action(ctx, sender, action, response.selection)
                continue
            if action not in CARD_ACTIONS:
                continue
            item = TextContent(text=CARD_ACTIONS[action])
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"session start {sender}")
            has_text = any(isinstance(c, TextContent) for c in msg.content)
            if not has_text and not (ctx.storage.get(sender) or {}).get("active"):
                await ctx.send(sender, reply(HELP, roles_card()))
        elif isinstance(item, TextContent):
            text_in = MENTION.sub("", item.text).strip()
            state = ctx.storage.get(sender) or {}
            if not state.get("active") and is_jd(text_in.removeprefix("interview")):
                await ctx.send(sender, reply("⏳ Priya is reading the job description and preparing your interview…"))
            try:
                text = await handle_text(ctx, sender, text_in)
                card = None
                if isinstance(text, tuple):
                    text, card = text
                elif text == HELP:
                    card = roles_card()
            except BackendError as e:
                ctx.logger.error(str(e))
                text, card = "⚠️ The interview service is busy right now. Please send your message again in a moment.", None
            except Exception:  # noqa: BLE001
                ctx.logger.exception("handler failed")
                text, card = "⚠️ Something went wrong on my side. Please try again.", None
            await ctx.send(sender, reply(text, card))
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"session end {sender}")


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


def _unwatch(ctx: Context, sender: str) -> None:
    watch = ctx.storage.get("watch") or {}
    if watch.pop(sender, None) is not None:
        ctx.storage.set("watch", watch)


class RoomDone(Model):
    sid: str
    token: str


class RoomDoneAck(Model):
    delivered: bool


@agent.on_rest_post("/room-done", RoomDone, RoomDoneAck)
async def on_room_done(ctx: Context, req: RoomDone) -> RoomDoneAck:
    if not hmac.compare_digest(req.token, os.environ.get("BACKEND_TOKEN", "")):
        return RoomDoneAck(delivered=False)
    watch = ctx.storage.get("watch") or {}
    sender = next((s for s, sid in watch.items() if sid == req.sid), None)
    if sender is None:
        return RoomDoneAck(delivered=False)
    state = ctx.storage.get(sender) or {}
    _unwatch(ctx, sender)
    if state.get("sid") != req.sid or not state.get("active"):
        return RoomDoneAck(delivered=False)
    card = await call("room_result", sid=req.sid)
    ctx.storage.set(sender, {**state, "active": False})
    await ctx.send(sender, reply(f"🏁 **Your face-to-face interview is complete.**\n\n📋 **Your scorecard**\n\n{card}{PREP_PACK_OFFER}", scorecard_card()))
    return RoomDoneAck(delivered=True)


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
    await ctx.send(sender, reply("✅ Payment received. ⏳ Preparing your Prep Pack…"))
    try:
        pack = await call("prep_pack", sid=order["sid"])
        text = f"💎 **Your Prep Pack**\n\n{pack}"
    except BackendError as e:
        ctx.logger.error(str(e))
        text = "Your Prep Pack is taking longer than usual. You've already paid, so reply **prep pack** in a moment to receive it at no extra charge."
        ctx.storage.set(f"pay:{msg.reference}", {**order, "paid": True, "tx": msg.transaction_id, "delivered": False})
    await ctx.send(sender, reply(text))


@payments.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    await ctx.send(sender, reply("No problem, the Prep Pack is still available anytime. Reply **prep pack** to unlock it."))


agent.include(chat, publish_manifest=True)
agent.include(payments, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
