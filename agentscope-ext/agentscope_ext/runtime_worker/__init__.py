"""Sandbox-side AgentScope worker.

B-M2: the worker boots a real ``ReActAgent`` and runs one task.  All
behaviour that needs to vary per deployment (LLM routing, MCP
injection, packaged Skills, audit forwarding) is gated through
:mod:`agentscope_ext.runtime_worker.hooks` so the sibling extensions
can override it without forking this package.

See ``__main__.py`` for the wire contract with ``agent-scheduler``.
"""
