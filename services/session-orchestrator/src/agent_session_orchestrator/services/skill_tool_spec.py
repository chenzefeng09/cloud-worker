"""Inline-path skill tool spec — the OpenAI ``read_skill`` contract + catalog prompt.

Background
----------
Skills are pure **instruction packages** (a ``SKILL.md`` plus optional
``references/`` / ``scripts/`` assets).  On the **sandbox** path the worker
registers each installed skill in the system prompt as "name + description +
SKILL.md path" and lets the model ``Read`` the file on demand (progressive
disclosure); nothing is exec'd on registration.

The **inline** path has no sandbox filesystem and no ``Read`` tool, so it mirrors
the same progressive-disclosure shape over the gateway instead:

* a system-prompt catalog block lists the enabled skills (``- name：description``)
  and tells the model to pull the full instructions with ``read_skill`` before it
  acts on a skill that matches the task;
* a single ``read_skill`` OpenAI function fetches ``SKILL.md`` (or any attached
  reference file) from skill-store in-process.

This module is the **single source of truth for the inline path** — it holds the
OpenAI function schema and the catalog-prompt builder, keeping them out of
``inline_run_executor`` so every caller imports the same definition (the same
pattern ``canvas_tool_spec`` follows for ``create_canvas``).

Boundary
--------
Inline runs cannot execute scripts.  The catalog block therefore states plainly
that when a skill's instructions call for running ``scripts/`` the model must
either complete the task with an equivalent pure-instruction approach or suggest
switching to agent (sandbox) mode — the model must never pretend it ran a script.
"""

from __future__ import annotations

from typing import Any

# The OpenAI tool name for the inline skill reader.  Distinct from the sandbox
# ``Read`` tool (which is a generic filesystem read); ``read_skill`` is a
# skill-store-backed fetch scoped to a single installed skill's package files.
READ_SKILL_TOOL_NAME = "read_skill"

# Default file every skill package is required to carry — the entry point the
# catalog block points the model at.
DEFAULT_SKILL_FILE = "SKILL.md"

# The OpenAI ``tools=[…]`` entry advertised to the gateway when at least one
# skill is in the run's catalog.  ``read_skill`` fetches a single package file
# (``SKILL.md`` by default, or a ``references/…`` attachment) so the model reads
# the full instructions on demand rather than having every skill's body inlined
# into the system prompt.
READ_SKILL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_SKILL_TOOL_NAME,
        "description": (
            "Read the full instructions of an installed skill on demand. The "
            "system prompt lists a catalog of available skills with one-line "
            "descriptions; when one of them is relevant to the current task, call "
            "this tool FIRST to fetch its complete SKILL.md instructions, then "
            "follow them to complete the task. You may also read a skill's "
            "attached reference files (e.g. references/foo.md) by passing their "
            "relative path. Do not guess a skill's contents from its one-line "
            "description — read it. Only call this for skills that appear in the "
            "catalog."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The exact skill name as listed in the catalog block of "
                        "the system prompt."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path of the file to read within the skill "
                        "package. Defaults to 'SKILL.md' (the main instructions). "
                        "Use a path like 'references/style.md' to read an "
                        "attached reference file the SKILL.md points to."
                    ),
                    "default": DEFAULT_SKILL_FILE,
                },
            },
            "required": ["name"],
        },
    },
}


def build_skills_catalog_prompt(entries: list[dict[str, Any]]) -> str:
    """Build the Chinese catalog system-prompt block for the inline skill loop.

    ``entries`` is the resolved skill catalog — a list of dicts each carrying at
    least ``name`` and (best-effort) ``description``.  The returned block lists
    every skill as ``- name：description`` and instructs the model to pull the
    full instructions with ``read_skill`` when a listed skill matches the task,
    plus the hard boundary that inline mode cannot run ``scripts/``.

    Returns an empty string when ``entries`` is empty (no catalog → no block,
    matching the "tool and instruction appear/disappear together" gating the
    canvas block uses).  Style mirrors ``canvas_tool_spec.CANVAS_FORCING_PROMPT``.
    """
    if not entries:
        return ""

    lines: list[str] = []
    for entry in entries:
        name = str((entry or {}).get("name") or "").strip()
        if not name:
            continue
        desc = str((entry or {}).get("description") or "").strip()
        lines.append(f"- {name}：{desc}" if desc else f"- {name}")

    if not lines:
        return ""

    catalog = "\n".join(lines)
    return (
        "\n\n## 可用技能（read_skill 按需读取）\n"
        "你已安装以下技能（每条为「名称：说明」）：\n"
        f"{catalog}\n"
        "当目录里的某个技能与当前任务相关时，"
        "**先调用 read_skill 工具**（传入该技能的名称）读取它的完整 SKILL.md 指令，"
        "再按指令完成任务；不要仅凭一行说明去猜测技能内容。"
        "如需读取技能附带的参考文件（如 SKILL.md 指向的 references/ 下文件），"
        "把相对路径作为 path 参数传入。\n"
        "边界声明：当前为会话（inline）模式，**无法执行脚本**。"
        "若某技能的指令要求运行 scripts/ 下的脚本，"
        "请改用纯指令的等价方式完成任务，"
        "或建议用户切换到「智能体模式」来运行该脚本；"
        "**严禁**假装已经运行了脚本或伪造脚本的运行结果。"
    )
