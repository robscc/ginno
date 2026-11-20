"""Session WebSocket endpoint: the receive loop and the title touch.

Split out of the former single-module ``api/stream/ws.py`` (batch 4-2,
structural only — no behavior change). The package ``__init__`` re-exports
the whole historical namespace and mirrors patch writes into the
submodules, so ``ginno_runtime.api.stream.ws.X`` (and the parent package's
``from .ws import X``) keeps working as the import/patch surface:

- :mod:`.handshake` — connection opening: delegation gate, session
  resolution, accept, file watcher, pending-interrupt re-emit; produces
  the :class:`WsConn` state bundle.
- :mod:`.loop` — the endpoint + receive loop (the single message pump);
  lightweight stateless branches inline.
- :mod:`.invoke` — the ``invoke`` branch: turn start, builtin/mod
  commands, busy gate, ``_turn_job`` spawn.
- :mod:`.routing` — @session deliveries + AgentTeam mention/plaza routes.
- :mod:`.steer` — ``steer`` / ``steer.recall`` / ``permission_response`` /
  ``user_answer``.
- :mod:`.control` — ``stop`` / subagent plan confirm/cancel / checkpoint
  retry.
- :mod:`.closing` — disconnect teardown.

Two structural contracts keep the outside world unchanged (mirroring the
``api/stream`` package split):

1. **Import surface** — every name the old single module carried is
   re-exported here, so ``from ginno_runtime.api.stream.ws import X`` and
   attribute reads keep working verbatim.
2. **Patch surface** — tests monkeypatch attributes on this package or on
   the parent ``ginno_runtime.api.stream`` package. The parent's mirror
   forwards writes into this package; :class:`_PatchMirrorModule` below
   then mirrors them into each submodule whose namespace carries the
   name (and monkeypatch's restore mirrors back the same way).
"""

from __future__ import annotations

import sys
from types import ModuleType

from .... import commands as _commands  # noqa: F401  (re-export)
from .... import files as files_mod  # noqa: F401  (re-export)
from .... import server_shared as shared  # noqa: F401  (re-export)
from .... import vibe_island as _vibe_island  # noqa: F401  (re-export)
from ....checkpointer import ABANDONED_TURNS  # noqa: F401  (re-export)
from ....goals import store as goal_store  # noqa: F401  (re-export)
from ....server_shared import (  # noqa: F401  (re-export)
    _MANAGED_TURNS,
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _TURN_STOP,
    _TURN_TASKS,
    _ev,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)
from ....session_meta import _find_meta, _session_meta_patch  # noqa: F401  (re-export)
from ....subagent_plan import (  # noqa: F401  (re-export)
    BACKGROUND_ASYNC_COMMANDS as _BACKGROUND_ASYNC_COMMANDS,
)
from ... import sessions as sessions_mod  # noqa: F401  (re-export)
from ...sessions import (  # noqa: F401  (re-export)
    _emit_goal_event,
    _ensure_session,
    _first_agent_id,
    _start_goal_driver,
)
from ..engine import _stop_parked_turn, _stream_graph, turn_error_fields  # noqa: F401  (re-export)
from ...messages_ui import skill_display_text  # noqa: F401  (re-export)
from ..resume import resume_pending_interrupt  # noqa: F401  (re-export)
from ..turn import (  # noqa: F401  (re-export)
    _attach_only_text,
    _prepare_steer_payload,
    _run_resume,
    _run_stream,
)
from .closing import close_connection  # noqa: F401  (re-export)
from .handshake import WsConn, open_connection  # noqa: F401  (re-export)
from .invoke import _touch_session_title, handle_invoke  # noqa: F401  (re-export)
from .loop import router, run_receive_loop, session_ws  # noqa: F401  (re-export)
from .routing import (  # noqa: F401  (re-export)
    _deliver_turn,
    handle_plan_deliveries,
    handle_plaza_route,
    handle_team_mention,
)
from .steer import (  # noqa: F401  (re-export)
    _ANSWERED_STEERS,
    _parked_user_question,
    handle_permission_response,
    handle_steer,
    handle_steer_recall,
    handle_user_answer,
)

_MIRROR_TARGETS = (
    sys.modules[__name__ + ".handshake"],
    sys.modules[__name__ + ".loop"],
    sys.modules[__name__ + ".invoke"],
    sys.modules[__name__ + ".routing"],
    sys.modules[__name__ + ".steer"],
    sys.modules[__name__ + ".control"],
    sys.modules[__name__ + ".closing"],
)


class _PatchMirrorModule(ModuleType):
    """Mirror attribute writes on this package into the submodules.

    Same contract as the parent ``api.stream`` package: a
    ``monkeypatch.setattr`` landing here (either directly or forwarded by
    the parent's mirror) is re-applied to every submodule whose namespace
    carries the name, so handler code reading plain module globals sees
    the patch exactly as it did against the monolith. ``__delattr__``
    mirrors restores the same way.
    """

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for _sub in _MIRROR_TARGETS:
            if name in vars(_sub):
                setattr(_sub, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for _sub in _MIRROR_TARGETS:
            if name in vars(_sub):
                delattr(_sub, name)


sys.modules[__name__].__class__ = _PatchMirrorModule
