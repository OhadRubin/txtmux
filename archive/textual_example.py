#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "textual[syntax]>=0.79.1",
#   "claude-agent-sdk",
#   "dicttoxml",
# ]
# requires-python = ">=3.11"
# ///

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Any, AsyncIterator, Callable

from xml.dom.minidom import parseString

import dicttoxml
from rich.console import Console, ConsoleOptions, RenderResult
from rich.padding import Padding
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import Reactive, reactive
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import Worker

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
    UserMessage,
)

ERROR_NOTIFY_TIMEOUT_SECS = 15


class FlowAction(Enum):
    """Actions that control prompt flow execution."""

    CONTINUE = "continue"
    BREAK = "break"


@dataclass(frozen=True)
class Prompt:
    """A prompt with optional callback for flow control."""

    id: str
    text: str
    callback: Callable[[str | None], FlowAction] | None = None


class CommandAction(Enum):
    STOP = "stop"
    STATUS = "status"
    SKIP = "skip"
    JUMP = "jump"
    QUERY = "query"
    PAUSE = "pause"
    UNPAUSE = "unpause"
    HELP = "help"


@dataclass(frozen=True)
class ParsedCommand:
    action: CommandAction | None
    arg: str | None
    raw: str


def parse_command(cmd: str, *, prompt_ids: set[str]) -> ParsedCommand:
    """Parse a raw command string into a structured command.

    Supports shorthand:
        <prompt_id> -> jump <prompt_id>
    """
    raw = cmd.strip()
    if not raw:
        return ParsedCommand(None, None, raw)

    parts = raw.split(maxsplit=1)
    action_word = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else None

    if action_word in CommandAction._value2member_map_:
        return ParsedCommand(CommandAction(action_word), arg, raw)

    if action_word in prompt_ids:
        return ParsedCommand(CommandAction.JUMP, action_word, raw)

    return ParsedCommand(None, arg, raw)


def get_terminal_size() -> tuple[int, int]:
    """Get terminal size (lines, columns)."""
    size = shutil.get_terminal_size()
    return size.lines, size.columns


def truncate_value(value: Any, *, max_lines: int, wrap_width: int) -> Any:
    """Wrap and truncate nested values, primarily to keep logs readable."""
    if isinstance(value, str):
        wrapped_lines: list[str] = []
        for line in value.splitlines() or [""]:
            if line.strip():
                wrapped_lines.extend(textwrap.wrap(line, width=wrap_width) or [""])
            else:
                wrapped_lines.append("")

        if len(wrapped_lines) > max_lines:
            truncated = "\n".join(wrapped_lines[:max_lines])
            return f"{truncated}\n... ({len(wrapped_lines) - max_lines} lines truncated)"
        return "\n".join(wrapped_lines)

    if isinstance(value, dict):
        return {
            str(k): truncate_value(v, max_lines=max_lines, wrap_width=wrap_width)
            for k, v in value.items()
        }

    if isinstance(value, list):
        return [
            truncate_value(item, max_lines=max_lines, wrap_width=wrap_width)
            for item in value
        ]

    return value


def filter_null_fields(data: Any) -> Any:
    """Recursively remove None fields from dicts."""
    if isinstance(data, dict):
        return {k: filter_null_fields(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [filter_null_fields(item) for item in data]
    return data


def safe_asdict(obj: Any, *, truncate: bool = False) -> dict[str, Any]:
    """Convert arbitrary objects to a JSON-safe dict.

    Claude SDK messages are dataclasses today, but we keep a robust fallback so
    logging never crashes the TUI.
    """
    try:
        if is_dataclass(obj):
            data: Any = asdict(obj)
        elif hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
            data = obj.model_dump()  # type: ignore[no-any-return]
        elif hasattr(obj, "__dict__"):
            data = dict(obj.__dict__)
        else:
            data = {"repr": repr(obj)}
    except Exception as exc:
        data = {"repr": repr(obj), "_asdict_error": str(exc)}

    if truncate:
        lines, cols = get_terminal_size()
        max_lines = max(5, lines // 3)
        wrap_width = max(40, cols - 10)
        data = truncate_value(data, max_lines=max_lines, wrap_width=wrap_width)

    # JSON round-trip to ensure serializable types (default=str) and to avoid
    # Rich/DictToXml choking on e.g. datetime objects.
    try:
        json_safe = json.loads(json.dumps(data, default=str))
        return json_safe if isinstance(json_safe, dict) else {"value": json_safe}
    except Exception as exc:
        return {"repr": repr(data), "_json_error": str(exc)}


def dict_to_pretty_xml(data: dict[str, Any]) -> str:
    """Convert a dict to pretty-printed XML, filtering out null fields."""
    filtered = filter_null_fields(data)
    xml_bytes = dicttoxml.dicttoxml(
        filtered,
        attr_type=False,
        custom_root="message",
    )
    dom = parseString(xml_bytes)
    pretty = dom.toprettyxml(indent="  ")

    lines = [line for line in pretty.splitlines() if line.strip()]
    if lines and lines[0].startswith("<?xml"):
        lines = lines[1:]
    return "\n".join(lines)


def get_pending_subphases(plan_path: str | Path) -> list[str]:
    """Return list of pending subphase IDs from a plan XML.

    A "subphase" is a <phase> nested inside another <phase>.
    Pending means status != "completed".
    """
    plan_file = Path(plan_path)
    if not plan_file.exists():
        raise FileNotFoundError(f"Plan file not found: {plan_file}")

    tree = ET.parse(plan_file)
    root = tree.getroot()

    pending: list[str] = []
    for subphase in root.findall(".//phase/phase"):
        status = subphase.get("status", "pending")
        if status != "completed":
            sub_id = subphase.get("id")
            if sub_id:
                pending.append(sub_id)
    return pending


def get_total_subphases(plan_path: str | Path) -> int:
    plan_file = Path(plan_path)
    tree = ET.parse(plan_file)
    root = tree.getroot()
    return len(root.findall(".//phase/phase"))


def get_plan_progress(plan_path: str | Path) -> tuple[int, int, list[str]]:
    """Return (completed, total, pending_ids)."""
    pending = get_pending_subphases(plan_path)
    total = get_total_subphases(plan_path)
    completed = max(0, total - len(pending))
    return completed, total, pending


def is_plan_complete(plan_path: str | Path) -> bool:
    _completed, _total, pending = get_plan_progress(plan_path)
    return len(pending) == 0


def get_prompts(plan_path: str) -> list[Prompt]:
    """Return list of prompts for the agent."""
    return [
        Prompt(
            id="identify-files",
            text=dedent(
                """\
                What were the files that were added, changed or modified in the last 5-7 commits?
                (ignoring commits tagged with <ignore_me> or have a commit message of dot/s)"""
            ),
        ),
        Prompt(
            id="read-plan",
            text=dedent(
                f"""\
                Read {plan_path}. READ THE ENTIRE FILE.
                Based on your holistic understanding of the next uncompleted (pending/partial status counts too, we recently had a partial subphase added in one of the first phases) subphase, also consider the main phase we are currently in.
                Based on previous 5-7 commits, do we need to update the <relevant_files> tag in {plan_path}?
                If so, do it.
                Essentially, i'm asking which of these files are relevant to the next uncompleted subphase in {plan_path}.
                Only read {plan_path} to try and answer this question. Make an educated guess.
                Do not edit any code yet."""
            ),
        ),
        Prompt(
            id="report-line-ranges",
            text=dedent(
                f"""\
                What is the exact line ranges for the current uncompleted phase in {plan_path}?
                How about the line range for the context and motivation in {plan_path}?"""
            ),
        ),
        Prompt(
            id="read-relevant-files",
            text=dedent(
                f"""\
                Read only the files you are **sure** are relevant (for both phase and subphase). Deploy a haiku agent per each
                file you aren't sure about, ask each agent what other files might be relevant to the subphase.
                Tell each agent to read that file, and the {plan_path} *just* enough to understand the context and motivation of the plan and the exact lines for the phase AND subphase.
                Instruct the agents to be very very concise in their final response to you.
                Deploy between 0-10 agents."""
            ),
        ),
        Prompt(
            id="implement-subphase",
            text=dedent(
                f"""\
                Your goal is to implement the next **subphase** in {plan_path}.
                1. Look for the next **subphase** in {plan_path} that has status!="completed".
                2. Go into plan mode (DO NOT USE THE Plan agent, do it yourself!), and explore
                   the repo as you normally do in plan mode (exploring both the phase and subphase relevant files), using haiku Explore agents (optional).
                3. Write your plan.
                4. Exit plan mode.
                5. (Optional) Deploy haiku Explore agents. This time, use the things you learned from the other agents to fine-tune your understanding of the codebase (same methodology as above).
                   - Do this in order to gain exact line-numbers and file paths, so you wouldn't have to read so much.
                   - Your context length is more valuable than the Explore agents.
                6. Implement the plan using the information the Explore agents provided you."""
            ),
        ),
        Prompt(
            id="run-tests",
            text="If the plan involved writing or modifying code, did you test everything you could?",
        ),
        Prompt(
            id="check-missed-tests",
            text="Are you sure you didn't forget to test something else that you could have tested?",
        ),
        Prompt(
            id="finalize",
            text=dedent(
                f"""\
                ok.
                1. Set status="completed", and add tags explaining what you did in {plan_path} (2 lines max).
                2. Validate that {plan_path} is still valid xml using python's xml.etree.ElementTree.
                3. Commit and push"""
            ),
        ),
        Prompt(
            id="next-subphase-preview",
            text="what's the next subphase? do not execute any tools yet.",
        ),
        Prompt(
            id="confirm-files-read",
            text='Have you read all the required files for that next subphase? answer only with "Yes" or "No". Do not include any other text in your answer.',
            callback=lambda r: FlowAction.CONTINUE
            if r and "yes" in r.lower()
            else FlowAction.BREAK,
        ),
        Prompt(
            id="check-complexity",
            text='What is the complexity of that subphase? answer only with "High", "Medium" or "Low". Do not include any other text in your answer.',
            callback=lambda r: FlowAction.CONTINUE
            if r and "low" in r.lower()
            else FlowAction.BREAK,
        ),
        Prompt(id="execute-subphase", text="ok, please implement the next subphase"),
    ]


async def auto_approve(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow:
    """Auto-approve all tool requests."""
    return PermissionResultAllow()


async def prompt_stream(prompts: list[str]) -> AsyncIterator[dict[str, Any]]:
    """Wrap prompt strings as the async iterable expected by ClaudeSDKClient.query()."""
    for p in prompts:
        yield {"type": "user", "message": {"role": "user", "content": p}}


class ResponseStatus(Vertical):
    """A widget that displays the status of the response from the agent.

    Copied (with minimal changes) from Elia's `elia_chat/widgets/agent_is_typing.py`.
    """

    message: Reactive[str] = reactive("Agent is responding", recompose=True)

    def compose(self) -> ComposeResult:
        yield Label(f" {self.message}")
        yield LoadingIndicator()

    def set_awaiting_response(self) -> None:
        self.message = "Awaiting response"
        self.add_class("-awaiting-response")
        self.remove_class("-agent-responding")

    def set_agent_responding(self) -> None:
        self.message = "Agent is responding"
        self.add_class("-agent-responding")
        self.remove_class("-awaiting-response")


class PromptStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


PROMPT_STATUS_DECORATION: dict[PromptStatus, tuple[str, str]] = {
    PromptStatus.PENDING: ("○", "dim"),
    PromptStatus.IN_PROGRESS: ("→", "yellow"),
    PromptStatus.COMPLETED: ("✓", "green"),
    PromptStatus.SKIPPED: ("↷", "yellow"),
    PromptStatus.FAILED: ("✗", "red"),
}


def _prompt_preview(text: str, max_chars: int) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return textwrap.shorten(stripped, width=max_chars, placeholder="…")
    return ""


@dataclass
class PromptListItemRenderable:
    prompt: Prompt
    status: PromptStatus

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        icon, icon_style = PROMPT_STATUS_DECORATION[self.status]
        icon_text = Text(icon, style=icon_style)
        title_text = Text(self.prompt.id, style="bold")

        preview_width = max(20, options.max_width - 6)
        preview = _prompt_preview(self.prompt.text, max_chars=preview_width)
        preview_text = Text(preview, style="dim")

        yield Padding(
            Text.assemble(icon_text, " ", title_text, "\n", preview_text), pad=(0, 0, 0, 1)
        )


class PromptOption(Option):
    def __init__(self, prompt_data: Prompt, status: PromptStatus) -> None:
        super().__init__(PromptListItemRenderable(prompt_data, status))
        self.prompt_data = prompt_data
        self.status = status


class PromptList(OptionList):
    """Left-hand prompt list, modelled after Elia's ChatList widget."""

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("g,home", "first", "First", show=False),
        Binding("G,end", "last", "Last", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
    ]

    @dataclass
    class PromptActivated(Message):
        prompt_id: str

    def __init__(
        self,
        prompts: list[Prompt],
        status_map: dict[str, PromptStatus],
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._prompts = prompts
        self._status_map = status_map

    def on_mount(self) -> None:
        self.refresh_options()

    def refresh_options(
        self, *, keep_highlight: bool = True, highlighted: int | None = None
    ) -> None:
        prev_highlighted = self.highlighted if keep_highlight else None
        if highlighted is not None:
            prev_highlighted = highlighted

        self.clear_options()
        self.add_options(
            [
                PromptOption(p, self._status_map.get(p.id, PromptStatus.PENDING))
                for p in self._prompts
            ]
        )

        self.border_title = f"Prompts ({self.option_count})"
        if self.option_count and prev_highlighted is not None:
            self.highlighted = min(prev_highlighted, self.option_count - 1)
        self.border_subtitle = self.get_border_subtitle()

    def get_border_subtitle(self) -> str:
        if self.highlighted is None or self.option_count == 0:
            return ""
        return f"{self.highlighted + 1} / {self.option_count}"

    @on(OptionList.OptionHighlighted)
    @on(events.Focus)
    def _updateget_border_subtitle(self) -> None:
        if self.option_count and self.highlighted is None:
            self.highlighted = 0
        self.border_subtitle = self.get_border_subtitle()

    def on_blur(self) -> None:
        self.border_subtitle = ""

    @on(OptionList.OptionSelected)
    def _activate_prompt(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option, PromptOption):
            self.post_message(self.PromptActivated(prompt_id=event.option.prompt_data.id))


@dataclass(frozen=True)
class QueryResult:
    action: str  # "done", "skip", "jump"
    result: str | None = None
    session_id: str | None = None
    jump_to: str | None = None


class PlanAgentApp(App[None]):
    """A single-file Textual TUI for running `implement_plan.py`-style prompts.

    UI and interaction patterns are intentionally modelled after Elia:
    - Left list uses OptionList renderables (like `ChatList`)
    - ResponseStatus overlay (like `ResponseStatus` in Elia)
    - Keyboard-centric bindings and a bottom command input
    """

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
        padding: 0 2 1 2;
    }

    #main {
        height: 1fr;
    }

    #left-panel {
        width: 32;
    }

    #right-panel {
        width: 1fr;
    }

    #prompt-list {
        height: 1fr;
        border: round $primary 60%;
        padding: 0;
        background: $background 10%;
    }

    #run-status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }

    #message-log {
        height: 1fr;
        border: round $secondary 60%;
        padding: 1;
        background: $background 0%;
    }

    #command-bar {
        dock: bottom;
        height: 3;
        padding: 0 1;
        border: round $primary 60%;
        background: $background 0%;
    }

    #command-input {
        width: 1fr;
    }

    ResponseStatus {
        dock: top;
        align-horizontal: right;
        display: none;
        layer: overlay;
        height: 2;
        width: auto;
        margin-top: 1;
        margin-right: 1;
    }

    ResponseStatus Label {
        width: auto;
    }

    ResponseStatus LoadingIndicator {
        width: auto;
        height: 1;
        dock: right;
        margin-top: 1;
        color: $primary;
    }

    ResponseStatus.-awaiting-response LoadingIndicator {
        color: $primary;
    }

    ResponseStatus.-agent-responding LoadingIndicator {
        color: $secondary;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", key_display="q"),
        Binding("p", "toggle_pause", "Pause", key_display="p"),
        Binding("s", "skip_prompt", "Skip", key_display="s"),
        Binding("?", "show_help", "Help", key_display="?"),
        Binding("escape", "focus_command", "Command", key_display="esc"),
        Binding("ctrl+l", "clear_log", "Clear log", key_display="^l"),
    ]

    is_paused: Reactive[bool] = reactive(False)
    iteration: Reactive[int] = reactive(0)
    current_prompt_id: Reactive[str] = reactive("")
    agent_session_id: Reactive[str | None] = reactive(None)

    def __init__(
        self,
        *,
        plan_path: str,
        model: str,
        resume_prompt_id: str | None,
        resume_session_id: str | None,
        max_iterations: int | None,
        add_dirs: list[str],
    ) -> None:
        super().__init__()
        self.plan_path = Path(plan_path).expanduser().resolve()
        self.model = model
        self.resume_prompt_id = resume_prompt_id
        self.resume_session_id = resume_session_id
        self.max_iterations = max_iterations
        self.add_dirs = add_dirs

        self.prompts = get_prompts(str(self.plan_path))
        self.prompt_ids = {p.id for p in self.prompts}

        self.prompt_statuses: dict[str, PromptStatus] = {
            p.id: PromptStatus.PENDING for p in self.prompts
        }

        # Command routing is modelled after implement_plan.py's two-queue design.
        self.inline_queue: asyncio.Queue[ParsedCommand] = asyncio.Queue()
        self.race_queue: asyncio.Queue[ParsedCommand] = asyncio.Queue()

        self._agent_worker: Worker | None = None

        # Cached plan progress (avoid parsing XML on every UI update).
        self._pending_subphases: list[str] = []
        self._subphase_total: int = 0

        # Guard reactive watchers until widgets are mounted.
        self._ui_ready = False

    # ---------- Widget accessors ----------

    @property
    def message_log(self) -> RichLog:
        return self.query_one("#message-log", RichLog)

    @property
    def command_input(self) -> Input:
        return self.query_one("#command-input", Input)

    @property
    def prompt_list(self) -> PromptList:
        return self.query_one("#prompt-list", PromptList)

    @property
    def response_status(self) -> ResponseStatus:
        return self.query_one(ResponseStatus)

    @property
    def run_status(self) -> Static:
        return self.query_one("#run-status", Static)

    # ---------- Compose / lifecycle ----------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield PromptList(self.prompts, self.prompt_statuses, id="prompt-list")
            with Vertical(id="right-panel"):
                yield ResponseStatus()
                yield Static("", id="run-status")
                yield RichLog(id="message-log", markup=False, highlight=False)
        with Horizontal(id="command-bar"):
            yield Input(
                placeholder="Commands: stop | status | skip | jump <id> | query <text> | pause | unpause | help",
                id="command-input",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Plan Agent TUI"
        self._ui_ready = True

        self._refresh_plan_progress_cache()
        self._update_status_widgets()

        # Focus the command input by default (keyboard-first).
        self.command_input.focus()

        # Start the agent loop.
        self._agent_worker = self.run_agent_loop()

    async def on_shutdown(self) -> None:
        if self._agent_worker is not None:
            self._agent_worker.cancel()

    # ---------- Reactive watchers ----------

    def watch_is_paused(self, value: bool) -> None:
        if self._ui_ready:
            self._update_status_widgets()

    def watch_iteration(self, value: int) -> None:
        if self._ui_ready:
            self._update_status_widgets()

    def watch_current_prompt_id(self, value: str) -> None:
        if self._ui_ready:
            self._update_status_widgets()

    def watch_agent_session_id(self, value: str | None) -> None:
        if self._ui_ready:
            self._update_status_widgets()

    # ---------- Logging helpers ----------

    def log_line(self, text: str, *, style: str | None = None) -> None:
        self.message_log.write(Text(text, style=style))

    def log_rule(self, title: str | None = None) -> None:
        if title:
            self.message_log.write(Text(f"── {title} " + "─" * 20, style="dim"))
        else:
            self.message_log.write(Text("─" * 40, style="dim"))

    def log_exception(self, exc: BaseException, *, context: str) -> None:
        self.log_line(f"[error] {context}: {exc}", style="red")
        self.notify(
            str(exc),
            title=context,
            severity="error",
            timeout=ERROR_NOTIFY_TIMEOUT_SECS,
        )

    # ---------- Status / help ----------

    def _refresh_plan_progress_cache(self) -> None:
        try:
            completed, total, pending = get_plan_progress(self.plan_path)
        except Exception as exc:
            # If we can't parse the plan, we can't really run.
            self.log_exception(exc, context="Plan XML error")
            self._pending_subphases = []
            self._subphase_total = 0
            return

        self._pending_subphases = pending
        self._subphase_total = total

        # Update header subtitle too.
        status = "paused" if self.is_paused else "running"
        current = self.current_prompt_id or "-"
        self.sub_title = (
            f"{self.plan_path.name} • {completed}/{total} subphases • "
            f"iter {self.iteration} • {status} • prompt {current}"
        )

    def _update_status_widgets(self) -> None:
        # Keep header subtitle updated.
        self._refresh_plan_progress_cache()

        paused = "PAUSED" if self.is_paused else "RUNNING"
        prompt = self.current_prompt_id or "-"
        session = self.agent_session_id or "-"
        pending_count = len(self._pending_subphases)
        total = self._subphase_total
        completed = max(0, total - pending_count)

        self.run_status.update(
            Text.assemble(
                ("Status: ", "dim"),
                (paused, "yellow" if self.is_paused else "green"),
                ("   Iteration: ", "dim"),
                (str(self.iteration), "bold"),
                ("   Prompt: ", "dim"),
                (prompt, "bold"),
                ("   Session: ", "dim"),
                (session, "bold"),
                ("   Plan: ", "dim"),
                (f"{completed}/{total} subphases", "bold"),
            )
        )

    def show_help(self) -> None:
        self.log_rule("Help")
        self.log_line("Commands:", style="bold")
        self.log_line("  stop            - Exit immediately")
        self.log_line("  status          - Show pending subphases")
        self.log_line("  skip            - Skip to next prompt")
        self.log_line("  jump <id>       - Jump to a specific prompt (or type <id> directly)")
        self.log_line("  query <text>    - Interrupt current response with a new query")
        self.log_line("  pause           - Pause execution (unpause to continue)")
        self.log_line("  unpause         - Resume after pause")
        self.log_line("  help            - Show this help")
        self.log_rule()

    def show_plan_status(self) -> None:
        self._refresh_plan_progress_cache()
        pending = self._pending_subphases
        total = self._subphase_total
        completed = max(0, total - len(pending))

        self.log_rule("Plan status")
        self.log_line(f"Plan: {self.plan_path}")
        self.log_line(f"Progress: {completed}/{total} subphases completed")
        if pending:
            self.log_line(f"Pending ({len(pending)}):", style="bold")
            for sub_id in pending[:25]:
                self.log_line(f"  - {sub_id}")
            if len(pending) > 25:
                self.log_line(f"  ... ({len(pending) - 25} more)")
        else:
            self.log_line("All subphases completed!", style="green")
        self.log_rule()

    # ---------- Command handling ----------

    @on(Input.Submitted)
    def _command_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-input":
            return
        raw = event.value.strip()
        event.input.value = ""
        if raw:
            self.handle_command(raw)

    @on(PromptList.PromptActivated)
    def _prompt_clicked(self, event: PromptList.PromptActivated) -> None:
        # Clicking/entering on a prompt acts like "jump <id>".
        self.handle_command(f"jump {event.prompt_id}")

    def handle_command(self, raw: str) -> None:
        parsed = parse_command(raw, prompt_ids=self.prompt_ids)

        if parsed.action is None:
            self.log_line(f"[unknown] {parsed.raw}", style="red")
            return

        # Commands handled immediately (no need to involve the agent worker).
        if parsed.action == CommandAction.HELP:
            self.show_help()
            return

        if parsed.action == CommandAction.STATUS:
            self.show_plan_status()
            return

        if parsed.action == CommandAction.STOP:
            self.action_quit()
            return

        # Validate / normalize before enqueue.
        if parsed.action == CommandAction.JUMP:
            target = parsed.arg
            if not target:
                self.log_line("Usage: jump <prompt_id>", style="red")
                return
            if target not in self.prompt_ids:
                self.log_line(f"Unknown prompt id: {target}", style="red")
                return
            parsed = ParsedCommand(CommandAction.JUMP, target, raw)

        if parsed.action == CommandAction.QUERY:
            if not parsed.arg:
                self.log_line("Usage: query <text>", style="red")
                return

        if parsed.action == CommandAction.PAUSE:
            self.is_paused = True

        if parsed.action == CommandAction.UNPAUSE:
            self.is_paused = False

        # Echo the command into the log.
        self.log_line(f"> {raw}", style="dim")

        # Route to both queues (implement_plan.py design).
        self.inline_queue.put_nowait(parsed)
        self.race_queue.put_nowait(parsed)

    # ---------- Key binding actions ----------

    def action_focus_command(self) -> None:
        self.command_input.focus()

    def action_clear_log(self) -> None:
        self.message_log.clear()

    def action_show_help(self) -> None:
        self.show_help()

    def action_toggle_pause(self) -> None:
        # Use command semantics so it also works during streaming.
        self.handle_command("unpause" if self.is_paused else "pause")

    def action_skip_prompt(self) -> None:
        self.handle_command("skip")

    def action_quit(self) -> None:
        if self._agent_worker is not None:
            self._agent_worker.cancel()
        self.exit()

    # ---------- Prompt status handling ----------

    def set_prompt_status(self, prompt_id: str, status: PromptStatus) -> None:
        self.prompt_statuses[prompt_id] = status
        self.prompt_list.refresh_options(keep_highlight=True)

    def reset_prompt_statuses(self, *, start_index: int) -> None:
        for idx, prompt in enumerate(self.prompts):
            if idx < start_index:
                self.prompt_statuses[prompt.id] = PromptStatus.SKIPPED
            else:
                self.prompt_statuses[prompt.id] = PromptStatus.PENDING
        self.prompt_list.refresh_options(keep_highlight=False, highlighted=start_index)

    # ---------- Claude message logging ----------

    def log_message(self, message: Any) -> None:
        truncate = isinstance(message, UserMessage)

        payload = {
            "type": type(message).__name__,
            **safe_asdict(message, truncate=truncate),
        }

        if isinstance(message, SystemMessage):
            return

        xml = dict_to_pretty_xml(payload)
        self.message_log.write(
            Syntax(
                xml,
                "xml",
                theme="monokai",
                word_wrap=True,
                line_numbers=False,
            )
        )

    # ---------- Agent execution ----------

    async def _wait_for_unpause(self) -> None:
        """Block until we see an 'unpause' command on the inline queue."""
        while True:
            cmd = await self.inline_queue.get()
            if cmd.action == CommandAction.UNPAUSE:
                return

    async def _interrupt_client(self, client: ClaudeSDKClient) -> None:
        with contextlib.suppress(Exception):
            await client.interrupt()

    async def send_query(
        self,
        client: ClaudeSDKClient,
        query: str,
        session_id: str | None,
    ) -> tuple[str | None, str | None]:
        """Send a query, streaming messages, supporting inline interrupts."""
        self.response_status.display = True
        try:
            current_query = query

            while True:
                self.response_status.set_awaiting_response()
                self.log_rule("Sending query")
                self.log_line(f"[prompt] {current_query}", style="cyan")

                try:
                    await client.query(prompt_stream([current_query]))
                except Exception as exc:
                    self.log_exception(exc, context="Failed to send query")
                    return None, session_id

                agent_started = False
                messages = client.receive_messages()

                while True:
                    message_task = asyncio.create_task(anext(messages))
                    command_task = asyncio.create_task(self.inline_queue.get())

                    done, _pending = await asyncio.wait(
                        {message_task, command_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if command_task in done:
                        cmd = command_task.result()

                        message_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await message_task

                        if cmd.action == CommandAction.QUERY and cmd.arg:
                            self.log_line(
                                f"[interrupt] new query: {cmd.arg}", style="yellow"
                            )
                            await self._interrupt_client(client)
                            with contextlib.suppress(Exception):
                                await messages.aclose()
                            current_query = cmd.arg
                            break  # restart outer loop

                        if cmd.action == CommandAction.PAUSE:
                            self.log_line(
                                "[paused] Type 'unpause' to continue…", style="yellow"
                            )
                            self.response_status.display = False
                            await self._interrupt_client(client)
                            with contextlib.suppress(Exception):
                                await messages.aclose()
                            await self._wait_for_unpause()
                            self.log_line("[unpaused]", style="green")
                            self.response_status.display = True
                            current_query = "Continue"
                            break  # restart outer loop

                        # Ignore irrelevant inline commands (skip/jump handled elsewhere).
                        continue

                    # message_task completed first
                    command_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await command_task

                    try:
                        message = message_task.result()
                    except StopAsyncIteration:
                        self.log_line(
                            "[error] message stream ended unexpectedly", style="red"
                        )
                        return None, session_id

                    if not agent_started and not isinstance(message, SystemMessage):
                        self.response_status.set_agent_responding()
                        agent_started = True

                    if getattr(message, "subtype", None) == "init":
                        session_id = getattr(message, "data", {}).get("session_id")
                        self.agent_session_id = session_id

                    self.log_message(message)

                    if isinstance(message, ResultMessage):
                        if message.subtype == "error_during_execution":
                            self.notify(
                                "Agent reported error_during_execution",
                                title="Agent error",
                                severity="error",
                                timeout=ERROR_NOTIFY_TIMEOUT_SECS,
                            )
                            return None, session_id
                        return message.result, session_id

        except asyncio.CancelledError:
            await self._interrupt_client(client)
            raise
        finally:
            self.response_status.display = False

    async def _wait_for_race_interrupt(self) -> ParsedCommand:
        """Wait for a command that should cancel the whole prompt (skip/jump)."""
        while True:
            cmd = await self.race_queue.get()
            if cmd.action in (CommandAction.SKIP, CommandAction.JUMP):
                return cmd
            # Drop everything else.

    async def query_with_interrupt(
        self,
        client: ClaudeSDKClient,
        prompt: Prompt,
        session_id: str | None,
    ) -> QueryResult:
        """Run a single prompt query, racing skip/jump against agent completion."""
        query_task = asyncio.create_task(self.send_query(client, prompt.text, session_id))
        interrupt_task = asyncio.create_task(self._wait_for_race_interrupt())

        done, _pending = await asyncio.wait(
            {query_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if interrupt_task in done:
            cmd = interrupt_task.result()

            query_task.cancel()
            await self._interrupt_client(client)
            with contextlib.suppress(asyncio.CancelledError):
                await query_task

            if cmd.action == CommandAction.SKIP:
                return QueryResult(action="skip")

            if cmd.action == CommandAction.JUMP:
                return QueryResult(action="jump", jump_to=cmd.arg)

            return QueryResult(action="skip")

        interrupt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await interrupt_task

        result, new_session_id = query_task.result()
        return QueryResult(action="done", result=result, session_id=new_session_id)

    async def run_single_iteration(
        self,
        *,
        resume_prompt_id: str | None,
        resume_session_id: str | None,
    ) -> str | None:
        """Run a single iteration. Returns prompt_id to resume from (jump), or None."""
        start_index = 0
        if resume_prompt_id is not None:
            try:
                start_index = [p.id for p in self.prompts].index(resume_prompt_id)
            except ValueError as exc:
                raise ValueError(f"Unknown prompt id: {resume_prompt_id}") from exc

        self.reset_prompt_statuses(start_index=start_index)

        options = ClaudeAgentOptions(
            can_use_tool=auto_approve,
            system_prompt={"type": "preset", "preset": "claude_code"},
            add_dirs=self.add_dirs,
            setting_sources=["user", "project", "local"],
            model=self.model,
        )

        session_id: str | None = resume_session_id

        for idx, prompt in enumerate(self.prompts[start_index:], start=start_index):
            while self.is_paused:
                await asyncio.sleep(0.1)

            self.current_prompt_id = prompt.id
            self.prompt_list.refresh_options(keep_highlight=False, highlighted=idx)
            self.set_prompt_status(prompt.id, PromptStatus.IN_PROGRESS)

            async with ClaudeSDKClient(options) as client:
                qres = await self.query_with_interrupt(client, prompt, session_id=session_id)

            if qres.action == "skip":
                self.set_prompt_status(prompt.id, PromptStatus.SKIPPED)
                continue

            if qres.action == "jump":
                self.set_prompt_status(prompt.id, PromptStatus.SKIPPED)
                return qres.jump_to

            session_id = qres.session_id
            options.resume = session_id

            if qres.result is None:
                self.set_prompt_status(prompt.id, PromptStatus.FAILED)
                self.log_line(
                    "[error] Prompt produced no result; ending iteration.", style="red"
                )
                return None

            self.set_prompt_status(prompt.id, PromptStatus.COMPLETED)

            if prompt.callback:
                flow_action = prompt.callback(qres.result)
                if flow_action == FlowAction.BREAK:
                    self.log_line("Flow action: BREAK", style="yellow")
                    return None

        return None

    @work(exclusive=True, group="agent-loop")
    async def run_agent_loop(self) -> None:
        self.log_rule("Agent loop")
        self.log_line("Starting agent loop…", style="green")

        resume_prompt_id = self.resume_prompt_id
        resume_session_id = self.resume_session_id

        self.iteration = 0

        try:
            while self.max_iterations is None or self.iteration < self.max_iterations:
                self._refresh_plan_progress_cache()

                if not self._pending_subphases:
                    self.log_line("All subphases completed! Plan is done.", style="green")
                    return

                self.iteration += 1
                self.log_line(
                    f"[Iteration {self.iteration}] {len(self._pending_subphases)} subphases remaining "
                    f"(next: {self._pending_subphases[0]})",
                    style="bold",
                )

                jump_to = await self.run_single_iteration(
                    resume_prompt_id=resume_prompt_id,
                    resume_session_id=resume_session_id,
                )

                resume_prompt_id = jump_to or None
                resume_session_id = None
                self.agent_session_id = None
                self.current_prompt_id = ""

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            self.log_line("Agent loop cancelled.", style="yellow")
            raise
        except Exception as exc:
            self.log_exception(exc, context="Agent loop crashed")
        finally:
            self.response_status.display = False
            self.log_line("Agent loop finished.", style="dim")


def show_status(plan_path: str) -> None:
    completed, total, pending = get_plan_progress(plan_path)
    print(f"Plan: {plan_path}")
    print(f"Progress: {completed}/{total} subphases completed")
    if pending:
        print(f"\nPending ({len(pending)}):")
        for p in pending:
            print(f"  - {p}")
    else:
        print("\nAll subphases completed!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan Agent TUI (Textual)")

    parser.add_argument("plan_path", type=str, help="Path to plan XML file")
    parser.add_argument("--status", action="store_true", help="Show plan status and exit")
    parser.add_argument("-m", "--model", type=str, help="Claude model name to use (e.g. opus)")
    parser.add_argument("--resume-prompt", type=str, help="Prompt id to resume from")
    parser.add_argument("--session", type=str, help="Session id for resume")
    parser.add_argument("--max-iterations", "-n", type=int, help="Max iterations to run")

    default_add_dir = os.path.expanduser("~/treebench_service")
    default_add_dirs = [default_add_dir] if Path(default_add_dir).exists() else []
    parser.add_argument(
        "--add-dir",
        action="append",
        default=default_add_dirs,
        help="Directory to add to Claude agent sandbox (can be repeated)",
    )

    args = parser.parse_args()

    if args.status:
        show_status(args.plan_path)
        return

    if not args.model:
        parser.error("-m/--model is required when running the TUI")

    app = PlanAgentApp(
        plan_path=args.plan_path,
        model=args.model,
        resume_prompt_id=args.resume_prompt,
        resume_session_id=args.session,
        max_iterations=args.max_iterations,
        add_dirs=args.add_dir,
    )
    app.run()


if __name__ == "__main__":
    main()