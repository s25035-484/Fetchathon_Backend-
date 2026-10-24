import hmac
import os
from pathlib import Path

from aiohttp import web
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))
import interview  # noqa: E402

TOKEN = os.environ["BACKEND_TOKEN"]
ROOM_HTML = (Path(__file__).parent / "room.html").read_text()

OPS = {
    "create_room": interview.create_room,
    "room_turn": interview.room_turn,
    "room_end": interview.room_end,
    "room_result": interview.room_result,
}


async def run(fn, kwargs):
    result = fn(**kwargs)
    if hasattr(result, "__await__"):
        result = await result
    return result


async def handle(request: web.Request) -> web.Response:
    if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {TOKEN}"):
        return web.json_response({"error": "unauthorized"}, status=401)
    fn = OPS.get(request.match_info["op"])
    if fn is None:
        return web.json_response({"error": "unknown op"}, status=404)
    try:
        return web.json_response({"result": await run(fn, await request.json())})
    except KeyError:
        return web.json_response({"error": "not found"}, status=404)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": type(e).__name__}, status=500)


def _room(request: web.Request) -> str:
    sid = request.match_info["sid"]
    if sid not in interview.ROOMS:
        raise web.HTTPNotFound(text="Interview not found or expired.")
    return sid


async def room_page(request: web.Request) -> web.Response:
    _room(request)
    return web.Response(text=ROOM_HTML, content_type="text/html")


async def room_state(request: web.Request) -> web.Response:
    return web.json_response(interview.room_state(_room(request)))


async def room_turn(request: web.Request) -> web.Response:
    sid = _room(request)
    body = await request.json()
    try:
        turn = await interview.room_turn(sid, str(body.get("text", "")))
        return web.json_response({"reply": turn["say"], "end": turn["end"]})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": type(e).__name__}, status=502)


async def room_end(request: web.Request) -> web.Response:
    sid = _room(request)
    try:
        return web.json_response({"scorecard": await interview.room_end(sid)})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": type(e).__name__}, status=502)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


app = web.Application(client_max_size=64 * 1024)
app.add_routes([
    web.post("/v1/{op}", handle),
    web.get("/health", health),
    web.get("/room/{sid}", room_page),
    web.get("/room/{sid}/state", room_state),
    web.post("/room/{sid}/turn", room_turn),
    web.post("/room/{sid}/end", room_end),
])

if __name__ == "__main__":
    web.run_app(app, port=int(os.environ.get("PORT", "8080")))
