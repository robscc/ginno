"""Space ownership states — ego-lite three-state contract.

``agent``                    Agent holds the Space and may eval JS.
``agentDelegatedToUser``     Handoff: Agent tools hard-stop; the human operates
                             the real page. Still *the agent's* Space.
``user``                     Human Space (Human-owned). Agent cannot complete
                             or steal it without an explicit claim.
"""

from __future__ import annotations

OWNER_AGENT = "agent"
OWNER_DELEGATED = "agentDelegatedToUser"
OWNER_USER = "user"

ALL_OWNERS = {OWNER_AGENT, OWNER_DELEGATED, OWNER_USER}

# Agent tools (eval / click / navigate) are hard-stopped in these states.
AGENT_LOCKED = {OWNER_DELEGATED, OWNER_USER}
