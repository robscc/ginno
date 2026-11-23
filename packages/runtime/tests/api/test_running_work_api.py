"""Running work reports while a Worker assignment executes in the background."""

from __future__ import annotations

import asyncio

from ginno_runtime import server_shared as shared
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.worker_bus import deliver


def test_running_report_keeps_long_worker_wake(client, create_session):
    model_entered = asyncio.Event()
    release_model = asyncio.Event()

    class HeldModel(ScriptedChatModel):
        async def _astream(self, *args, **kwargs):
            model_entered.set()
            await release_model.wait()
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    title = "Worker target"
    sid = create_session(HeldModel(scripts=[script(text="Wake complete.")]), title=title)

    async def start_assignment():
        existing_tasks = set(deliver._ASSIGN_BG_TASKS)
        receipt = await deliver.assign_to_worker(
            "sender", sid, "Process this assignment.", async_delivery=True,
        )
        assert receipt["ok"] and receipt["receipt"] == "queued", receipt
        (wake_task,) = deliver._ASSIGN_BG_TASKS - existing_tasks
        return wake_task

    wake_task = client.portal.call(start_assignment)

    async def backdate_live_turn():
        await asyncio.wait_for(model_entered.wait(), timeout=10)
        assert not wake_task.done()
        assert sid not in shared._TURN_TASKS
        shared._RUNNING_TURNS_AT[sid] -= shared.RUNNING_TURN_STALE_S + 5
        return shared._RUNNING_TURNS[sid]

    async def finish_assignment():
        release_model.set()
        assert await asyncio.wait_for(wake_task, timeout=10) == "ran"

    try:
        turn_id = client.portal.call(backdate_live_turn)
        response = client.get("/api/running")
        assert response.status_code == 200
        assert response.json()["sessions"] == [{"id": sid, "title": title}]
        assert client.get("/api/badge").json()["running"] == 1
        assert shared._RUNNING_TURNS[sid] == turn_id
    finally:
        client.portal.call(finish_assignment)

    assert client.get("/api/running").json()["sessions"] == []
    assert client.get("/api/badge").json()["running"] == 0
    assert (sid, turn_id) not in shared._WAKE_TURN_TASKS
