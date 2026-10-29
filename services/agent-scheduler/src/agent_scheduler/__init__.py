"""Agent Scheduler — spawns sandbox containers/VMs running the Claude Agent SDK worker.

Sandbox runtime is pluggable (see ``services/runtime.py``):

* ``docker`` — Docker containers for dev / CI (no KVM required)
* ``cube``   — CubeSandbox microVMs for production
* ``kata``   — Kata Containers fallback (reserved)

Each runtime spawns the same image and invokes the same entrypoint
(``node /app/dist/main.js`` from ``worker-claude-sdk``).  See ADR 0001 / ADR 0003.
"""

__version__ = "0.1.0a1"
