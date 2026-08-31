"""Manual e2e runner for delegate_agent (docs/external-agents-design.md §10).

NOT part of the test suite — invokes REAL external CLIs. Usage:
    uv run python e2e_delegate_check.py <backend> <prompt> [timeout] [mode]
"""

import os
import sys

os.environ.setdefault("GINNO_HOME", "/tmp/ginno-e2e-home")
os.environ["GINNO_EXTERNAL_AGENTS"] = "1"
os.makedirs("/tmp/ginno-e2e-home", exist_ok=True)

WS = "/tmp/ginno-e2e-ws"
os.makedirs(WS + "/sub", exist_ok=True)
for name, content in [
    ("a.txt", "hello"),
    ("b.md", "# notes\nworld"),
    ("sub/c.txt", "nested"),
]:
    with open(os.path.join(WS, name), "w") as f:
        f.write(content)

from ginno_runtime.tools.external_agent import build_external_agent_tools  # noqa: E402

tools = build_external_agent_tools(WS, session_id="e2e-s1", project_slug="e2e")
if not tools:
    print("[e2e] gate closed — no tools")
    sys.exit(1)
tool = tools[0]
backend = sys.argv[1]
prompt = sys.argv[2]
timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 120
mode = sys.argv[4] if len(sys.argv) > 4 else "read-only"
out = tool.invoke(
    {"backend": backend, "prompt": prompt, "timeout": timeout, "mode": mode}
)
print(out[:2000])
