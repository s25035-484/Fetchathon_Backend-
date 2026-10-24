"""Thin client for the hosted InterviewPilot logic API (BACKEND_URL)."""
import os

import httpx


class BackendError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


async def call(op: str, **kwargs):
    url = os.environ["BACKEND_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        r = await client.post(f"{url}/v1/{op}", json=kwargs,
                              headers={"Authorization": f"Bearer {os.environ['BACKEND_TOKEN']}"})
    if r.status_code != 200:
        raise BackendError(f"{op}: HTTP {r.status_code}", r.status_code)
    return r.json()["result"]
