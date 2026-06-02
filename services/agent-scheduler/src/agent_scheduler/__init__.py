"""Agent Scheduler — spawns sandbox containers/VMs running the AgentScope worker.

Sandbox runtime is pluggable (see ``services/runtime.py``):

* ``docker`` — Docker containers for dev / CI (no KVM required)
* ``cube``   — CubeSandbox microVMs for production
* ``kata``   — Kata Containers fallback (reserved)

Each runtime spawns the same image and invokes the same entrypoint
(``python -m agentscope_ext.runtime_worker``).  See ADR 0001 / ADR 0002.
"""

__version__ = "0.1.0a1"
