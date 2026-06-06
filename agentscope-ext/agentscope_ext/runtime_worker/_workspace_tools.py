"""Built-in workspace file tools for the runtime_worker sandbox.

Registers AgentScope's own production-grade read/search/write tools
(``Read``, ``Grep``, ``Glob``, ``Write``, ``Edit``, ``Bash``) into the
default :class:`~agentscope.tool.Toolkit`.  These tools are always available
to the agent so it can inspect, create, and edit files under
``/workspace/``.

The workspace directory is a bind-mounted volume inside an isolated container,
so there is no need for application-level path sandboxing — the container
boundary is the security perimeter.

AgentScope built-in tools used
-------------------------------
* ``Read``  — reads a file by absolute path (supports ``max_line_characters``).
* ``Grep``  — searches file content with a pattern.
* ``Glob``  — lists files matching a glob pattern.
* ``Write`` — writes (or overwrites) a file at an absolute path.
* ``Edit``  — performs exact string replacements in an existing file.
* ``Bash``  — executes a shell command and returns its output.

All six classes live in ``agentscope.tool`` (re-exported from
``agentscope.tool._builtin``).  Each is instantiated with its
zero-argument constructor and appended to ``toolkit.tool_groups[0].tools``.
If any individual class is unavailable (old AgentScope version / test stub)
its registration is skipped with a log warning so the worker still starts.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def register_into(toolkit: Any) -> None:
    """Register AgentScope built-in tools into *toolkit*.

    Appends ``Read``, ``Grep``, and ``Glob`` instances to
    ``toolkit.tool_groups[0].tools``.  Each tool is registered in its own
    try/except block so a missing class does not prevent the others from
    being registered.

    If ``toolkit.tool_groups`` does not exist (e.g. a minimal test stub), the
    function logs a warning and returns without raising.
    """
    try:
        tool_group = toolkit.tool_groups[0]
    except (AttributeError, IndexError) as exc:
        _log.warning(
            "cannot register built-in workspace tools: "
            "toolkit.tool_groups[0] unavailable (%s)",
            exc,
        )
        return

    _tool_specs = [
        ("Read", "agentscope.tool"),
        ("Grep", "agentscope.tool"),
        ("Glob", "agentscope.tool"),
        # Write/Edit/Bash are also classes in agentscope.tool._builtin,
        # re-exported via agentscope.tool.__init__.  They accept zero-arg
        # construction (dangerous_files/dangerous_directories default to the
        # AgentScope built-in lists, which is appropriate for the sandbox).
        # PermissionMode.BYPASS (set in __main__.py) auto-approves all
        # tool calls, so the sandbox never prompts the user.
        ("Write", "agentscope.tool"),
        ("Edit", "agentscope.tool"),
        ("Bash", "agentscope.tool"),
    ]

    for cls_name, module_name in _tool_specs:
        try:
            import importlib

            mod = importlib.import_module(module_name)
            cls = getattr(mod, cls_name)
            tool_group.tools.append(cls())
            _log.debug("registered built-in workspace tool: %s", cls_name)
        except ImportError:
            _log.warning(
                "skipping built-in tool %s: agentscope not installed", cls_name
            )
        except AttributeError:
            _log.warning(
                "skipping built-in tool %s: class not found in %s "
                "(agentscope version may be too old)",
                cls_name,
                module_name,
            )
        except Exception as exc:  # pragma: no cover
            _log.warning(
                "failed to register built-in tool %s: %s", cls_name, exc
            )
