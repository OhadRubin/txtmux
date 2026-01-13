# /// script
# dependencies = [
#   "claude-agent-sdk",
#   "dicttoxml",
#   "pygments",
#   "prompt_toolkit>=3.0",
# ]
# ///

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    PermissionResultAllow,
    ToolPermissionContext,
    query,
)
import argparse
import asyncio
import os
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Any, AsyncIterator, Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.completion import WordCompleter


class FlowAction(Enum):
    """Actions that control prompt flow execution."""
    CONTINUE = "continue"  # proceed to next prompt
    BREAK = "break"        # stop current iteration


@dataclass
class Prompt:
    """A prompt with optional callback for flow control."""
    id: str
    text: str
    callback: Callable[[str | None], FlowAction] | None = None

import json
from xml.dom.minidom import parseString

import dicttoxml
from pygments import highlight
from pygments.lexers import XmlLexer
from pygments.formatters import TerminalFormatter

"""
deloy haiku models in BFS mode that would help you add additional relevant files 
query_with_interrupt 1 agent per phase
each agent should read the plan, and suggest additional relevant files per subphase
here are additional directories that might be relevant
/home/ohadr/quick_compose
/home/ohadr/quick_compose_evalbox
/home/ohadr/treebench
enchourage them to search using rg+wc for general terms iterativly and narrow down their search. """



async def auto_approve(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow:
    """Auto-approve all tool requests."""
    return PermissionResultAllow()
    
async def fix_xml_with_claude(plan_file: Path, parse_error: ET.ParseError) -> ET.ElementTree:
    """Use Claude to fix malformed XML and return parsed tree."""
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Edit", "Bash"],
        permission_mode="acceptEdits",
        model="haiku",
        can_use_tool=auto_approve,
    )
    prompt = dedent(f"""\
        The XML file {plan_file} has a parse error: {parse_error}

        Please read {plan_file} and fix the XML such that it is valid.
        Use ET.parse to verify the fix works. Do not change the semantic content, only fix XML syntax issues.
        """)

    async for message in query(prompt=prompt, options=options):
        if hasattr(message, "result"):
            print(f"[fix_xml] {message.result}")

    return ET.parse(plan_file)


def parse_plan_xml(plan_file: Path) -> ET.ElementTree:
    """Parse plan XML, using Claude to fix if malformed."""
    try:
        return ET.parse(plan_file)
    except ET.ParseError as e:
        print(f"[parse_plan_xml] XML parse error: {e}")
        print(f"[parse_plan_xml] Attempting to fix with Claude...")
        return asyncio.get_event_loop().run_until_complete(fix_xml_with_claude(plan_file, e))


class InputController:
    """Async input handler using prompt_toolkit."""

    def __init__(self, prompt_ids: list[str]):
        self.prompt_ids = prompt_ids
        self.commands = ["stop", "status", "jump", "skip", "query", "pause", "unpause", "help"]
        completer = WordCompleter(self.commands + prompt_ids)
        self.session = PromptSession(completer=completer)
        self.inline_queue: asyncio.Queue[str] = asyncio.Queue()
        self.race_queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start background input loop."""
        self._task = asyncio.create_task(self._input_loop())

    async def stop(self):
        """Cancel input loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _input_loop(self):
        """Read input continuously, route commands to appropriate queue."""
        with patch_stdout(raw=True):
            while True:
                try:
                    cmd = await self.session.prompt_async("> ")
                    if cmd.strip():
                        await self._route_command(cmd.strip())
                except EOFError:
                    break

    async def _route_command(self, cmd: str):
        action, _ = self.parse_command(cmd)
        if action == "help":
            self.print_help()
        else:
            await self.inline_queue.put(cmd)
            await self.race_queue.put(cmd)

    def print_help(self):
        print("""
Commands:
  stop          - Exit immediately
  status        - Show pending subphases
  skip          - Skip to next prompt
  jump <id>     - Jump to specific prompt
  query <text>  - Send query to agent
  pause         - Pause execution (use 'unpause' to resume)
  unpause       - Resume after pause
  help          - Show this help
  <prompt_id>   - Shorthand for jump
""")

    def check_interrupt(self) -> str | None:
        """Non-blocking check for inline commands (used in send_query)."""
        try:
            return self.inline_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def parse_command(self, cmd: str) -> tuple[str, str | None]:
        """Parse command into (action, arg). Returns (cmd, None) for unknown."""
        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None

        if action in self.commands:
            return (action, arg)
        if action in self.prompt_ids:
            return ("jump", action)
        return (cmd, None)


def get_terminal_size() -> tuple[int, int]:
    """Get terminal size (lines, columns)."""
    import shutil
    size = shutil.get_terminal_size()
    return size.lines, size.columns


def truncate_value(value: Any, max_lines: int, wrap_width: int) -> Any:
    """Wrap and truncate a string value if it exceeds max_lines."""
    import textwrap

    if isinstance(value, str):
        # Wrap each line to terminal width
        wrapped_lines = []
        for line in value.splitlines():
            if line.strip():
                wrapped_lines.extend(textwrap.wrap(line, width=wrap_width) or [''])
            else:
                wrapped_lines.append('')

        if len(wrapped_lines) > max_lines:
            truncated = '\n'.join(wrapped_lines[:max_lines])
            return f"{truncated}\n... ({len(wrapped_lines) - max_lines} lines truncated)"
        return '\n'.join(wrapped_lines)
    elif isinstance(value, dict):
        return {k: truncate_value(v, max_lines, wrap_width) for k, v in value.items()}
    elif isinstance(value, list):
        return [truncate_value(item, max_lines, wrap_width) for item in value]
    return value


def filter_null_fields(data: Any) -> Any:
    """Recursively remove None/null fields from a dict."""
    if isinstance(data, dict):
        return {k: filter_null_fields(v) for k, v in data.items() if v is not None}
    elif isinstance(data, list):
        return [filter_null_fields(item) for item in data]
    return data


def safe_asdict(obj, truncate: bool = False) -> dict:
    """Convert dataclass to dict with optional truncation and JSON-safe fallback."""
    d = asdict(obj)

    if truncate:
        lines, cols = get_terminal_size()
        max_lines = max(5, lines // 3)
        wrap_width = max(40, cols - 10)  # Leave margin for XML indentation
        d = truncate_value(d, max_lines, wrap_width)

    # Ensure JSON-serializable by round-tripping with default=str
    return json.loads(json.dumps(d, default=str))


def dict_to_pretty_xml(data: dict) -> str:
    """Convert a dict to pretty-printed XML with syntax highlighting, filtering out null fields."""
    filtered = filter_null_fields(data)
    xml = dicttoxml.dicttoxml(filtered, attr_type=False)
    dom = parseString(xml)
    pretty_xml = dom.toprettyxml(indent='  ')
    return highlight(pretty_xml, XmlLexer(), TerminalFormatter())

def get_pending_subphases(plan_path: str) -> list[str]:
    """Return list of pending subphase IDs from plan XML.

    A subphase is a <phase> nested inside another <phase>.
    Pending means status != "completed".
    """
    plan_file = Path(plan_path)
    if not plan_file.exists():
        raise FileNotFoundError(f"Plan file not found: {plan_path}")

    tree = parse_plan_xml(plan_file)
    root = tree.getroot()

    pending = []
    for subphase in root.findall('.//phase/phase'):
        status = subphase.get('status', 'pending')
        if status != 'completed':
            pending.append(subphase.get('id'))

    return pending


def is_plan_complete(plan_path: str) -> bool:
    """Check if all subphases in plan are completed."""
    pending = get_pending_subphases(plan_path)
    return len(pending) == 0


def get_prompts(plan_path: str) -> list[Prompt]:
    """Return list of prompts for the agent."""
    return [
        Prompt(
            id="identify-files",
            text=dedent(f"""\
                What were the files that were added, changed or modified in the last 5-7 commits?
                (ignoring commits tagged with <ignore_me> or have a commit message of dot/s)"""),
        ),
        Prompt(
            id="read-plan",
            text=dedent(f"""\
                Read {plan_path}. READ THE ENTIRE FILE.
                Based on your holistic understanding of the next uncompleted (pending/partial status counts too, we recently had a partial subphase added in one of the first phases) subphase, also consider the main phase we are currently in.
                Based on previous 5-7 commits, do we need to update the <relevant_files> tag in {plan_path}?
                If so, do it.
                Essentially, i'm asking which of these files are relevant to the next uncompleted subphase in {plan_path}.
                Only read {plan_path} to try and answer this question. Make an educated guess.
                Do not edit any code yet."""),
        ),
        Prompt(
            id="report-line-ranges",
            text=dedent(f"""\
                What is the exact line ranges for the current uncompleted phase in {plan_path}?
                How about the line range for the context and motivation in {plan_path}?"""),
        ),
        Prompt(
            id="read-relevant-files",
            text=dedent(f"""\
                Read only the files you are **sure** are relevant (for both phase and subphase). Deploy a haiku agent per each
                file you aren't sure about, ask each agent what other files might be relevant to the subphase.
                Tell each agent to read that file, and the {plan_path} *just* enough to understand the context and motivation of the plan and the exact lines for the phase AND subphase.
                Instruct the agents to be very very concise in their final response to you.
                Deploy between 0-10 agents."""),
        ),
        Prompt(
            id="implement-subphase",
            text=dedent(f"""\
                Your goal is to implement the next **subphase** in {plan_path}.
                1. Look for the next **subphase** in {plan_path} that has status!="completed".
                2. Go into plan mode (DO NOT USE THE Plan agent, do it yourself!), and explore
                   the repo as you normally do in plan mode (exploring both the phase and subphase relevant files), using haiku Explore agents (optional).
                3. Write your plan.
                4. Exit plan mode.
                5. (Optional) Deploy haiku Explore agents. This time, use the things you learned from the other agents to fine-tune your understanding of the codebase (same methodology as above).
                   - Do this in order to gain exact line-numbers and file paths, so you wouldn't have to read so much.
                   - Your context length is morevaluable than the Explore agents.
                6. Implement the plan using the information the Explore agents provided you."""),
        ),
        Prompt(id="run-tests", text="Remind me what were the 'must pass'/'stretch' success criteria? Just list them, do not mention if you passed them or not, just give a simple list."),
        Prompt(id="run-tests", text="Did you pass the 'must pass' success criteria? If not, iterate until you pass them."),
        Prompt(id="check-missed-tests", text="Are you sure you didn't forget to test something else that you could have tested?"),
        Prompt(
            id="finalize",
            text=dedent(f"""\
                ok.
                1. Set status="completed", and add tags explaining what you did in {plan_path} (2 lines max).
                2. Validate that {plan_path} is still valid xml using python's xml.etree.ElementTree.
                3. Commit and push"""),
        ),
        Prompt(id="next-subphase-preview", text="what's the next subphase? do not execute any tools yet."),
        Prompt(
            id="confirm-files-read",
            text='Have you read all the required files for that next subphase? answer only with "Yes" or "No". Do not include any other text in your answer.',
            callback=lambda r: FlowAction.CONTINUE if r and "yes" in r.lower() else FlowAction.BREAK,
        ),
        Prompt(
            id="check-complexity",
            text='What is the complexity of that subphase? answer only with "High", "Medium" or "Low". Do not include any other text in your answer.',
            callback=lambda r: FlowAction.CONTINUE if r and "low" in r.lower() else FlowAction.BREAK,
        ),
        Prompt(id="execute-subphase", text="ok, please implement the next subphase"),
    ]




TEST_PROMPT = Prompt(
    id="test",
    text="""Go into plan mode, write "create 'Hello there' in a new file", exit plan mode and do what the plan says.""",
)




async def prompt_stream(prompts: list[str]) -> AsyncIterator[dict[str, Any]]:
    """Wrap prompts as async iterable.

    Args:
        prompts: List of prompt strings to yield one at a time.
    """
    for p in prompts:
        yield {"type": "user", "message": {"role": "user", "content": p}}


def handle_message(message):
    """Handle a single message from the agent - XML output."""
    truncate = isinstance(message, UserMessage)
    d = {"type": type(message).__name__, **safe_asdict(message, truncate=truncate)}

    if not isinstance(message, SystemMessage):
        print(dict_to_pretty_xml(d))


async def send_query(
    client: ClaudeSDKClient,
    query: str,
    session_id: Optional[str],
    controller: InputController,
) -> tuple[str | None, str | None]:
    """Send query. Returns (result, session_id)."""
    result_message = None
    current_query = query

    async def wait_for_unpause():
        while True:
            cmd = await controller.inline_queue.get()
            action, _ = controller.parse_command(cmd)
            if action == "unpause":
                return

    while result_message is None:
        print("\n--- Sending query ---\n")
        print("\n--- Sending query ---\n")
        print("\n--- Sending query ---\n")
        print(f"[query] {current_query}")
        print("\n--- Waiting for response ---\n")
        print("\n--- Waiting for response ---\n")
        print("\n--- Waiting for response ---\n")
        await client.query(prompt_stream([current_query]))
        interrupted = False

        async def consume_messages():
            nonlocal result_message, session_id
            async for message in client.receive_messages():
                if hasattr(message, 'subtype') and message.subtype == 'init':
                    session_id = message.data.get('session_id')
                handle_message(message)
                if isinstance(message, ResultMessage):
                    if message.subtype != "error_during_execution":
                        result_message = message
                    return True
                if interrupted:
                    return False
            return False

        consume_task = asyncio.create_task(consume_messages())

        while not consume_task.done():
            if cmd := controller.check_interrupt():
                action, arg = controller.parse_command(cmd)
                if action == "query":
                    interrupted = True
                    await client.interrupt()
                    await consume_task
                    current_query = arg
                    await client.query(prompt_stream([current_query]))
                    break
                elif action == "pause":
                    print("[paused] Type 'unpause' to continue...")
                    interrupted = True
                    await client.interrupt()
                    await consume_task
                    await wait_for_unpause()
                    print("[unpaused]")
                    current_query = "Continue"
                    
                    break
            await asyncio.sleep(0.1)
        

    return (result_message.result if result_message else None, session_id)

async def query_with_interrupt(
    client: ClaudeSDKClient,
    prompt: Prompt,
    session_id: Optional[str],
    controller: InputController,
    plan_path: str,
) -> tuple[str, tuple[str | None, str | None] | None]:
    """Run query with interrupt handling. Returns (action, result) where action is 'done'/'skip'/'jump'."""

    async def wait_for_interrupt() -> tuple[str, str | None]:
        while True:
            cmd = await controller.race_queue.get()
            action, arg = controller.parse_command(cmd)
            if action == "status":
                pending_phases = get_pending_subphases(plan_path)
                print(f"[status] Pending: {pending_phases[:3]}...")
            elif action == "stop":
                raise SystemExit(0)
            elif action in ("skip", "jump"):
                return (action, arg)

    query_task = asyncio.create_task(send_query(client, prompt.text, session_id, controller))
    interrupt_task = asyncio.create_task(wait_for_interrupt())

    done, _ = await asyncio.wait(
        [query_task, interrupt_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if interrupt_task in done:
        query_task.cancel()
        try:
            await client.interrupt()
        except Exception:
            pass
        try:
            await query_task
        except asyncio.CancelledError:
            pass

        action, arg = interrupt_task.result()
        if action == "skip":
            return ("skip", None)
        elif action == "jump":
            return ("jump", (arg, None))
    else:
        interrupt_task.cancel()

    return ("done", query_task.result())


async def run_single_iteration(
    prompts: list[Prompt],
    plan_path: str,
    model: str,
    resume_prompt_id: Optional[str],
    resume_session_id: Optional[str],
    controller: InputController,
) -> Optional[str]:
    """Run a single agent iteration. Returns prompt_id to resume from, or None."""
    start_index = 0
    if resume_prompt_id is not None:
        for i, prompt in enumerate(prompts):
            if prompt.id == resume_prompt_id:
                start_index = i
                break
        else:
            raise ValueError(f"unknown prompt id: {resume_prompt_id}")

    options = ClaudeAgentOptions(
        can_use_tool=auto_approve,
        system_prompt={"type": "preset", "preset": "claude_code"},
        add_dirs=[os.path.expanduser("~/treebench_service")],
        setting_sources=["user", "project", "local"],  # Load all settings

        model=model
    )

    session_id: Optional[str] = resume_session_id



    for prompt in prompts[start_index:]:
        async with ClaudeSDKClient(options) as client:
            action, payload = await query_with_interrupt(client, prompt, session_id, controller, plan_path)

            if action == "skip":
                continue
            elif action == "jump":
                return payload[0]

            result, session_id = payload
            options.resume = session_id
            if prompt.callback:
                flow_action = prompt.callback(result)
                if flow_action == FlowAction.BREAK:
                    print("Flow action: BREAK")
                    return None

    return None


async def run_agent(
    prompts: list[Prompt],
    plan_path: str,
    max_iterations: Optional[int],
    model: str,
    resume_prompt_id: Optional[str],
    resume_session_id: Optional[str],
) -> None:
    """Run agent with prompts."""
    controller = InputController([p.id for p in prompts])
    await controller.start()

    try:
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            pending = get_pending_subphases(plan_path)
            if not pending:
                print("All subphases completed! Plan is done.")
                break

            iteration += 1
            print(f"[Iteration {iteration}] {len(pending)} subphases remaining: {pending[0]}, ...")

            jump_to = await run_single_iteration(
                prompts,
                plan_path=plan_path,
                model=model,
                resume_prompt_id=resume_prompt_id,
                resume_session_id=resume_session_id,
                controller=controller,
            )
            if jump_to:
                resume_prompt_id = jump_to
            else:
                resume_prompt_id = None
            resume_session_id = None
            await asyncio.sleep(1)
    finally:
        await controller.stop()


async def main():
    parser = argparse.ArgumentParser(description="Run Claude agent to implement plan")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run test prompt (create Hello there file) once instead of plan loop",
    )
    parser.add_argument(
        "--max-iterations", "-n",
        type=int,
        help="Max iterations to run (default: infinite)",
    )
    parser.add_argument(
        "plan_path",
        type=str,
        help="Path to plan XML file",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show plan status (pending subphases) and exit",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default="opus",
        help="Model name to use",
    )
    parser.add_argument(
        "--resume-prompt",
        type=str,
        help="Prompt id to resume from",
    )
    parser.add_argument(
        "--session",
        type=str,
        help="Session id for resume",
    )

    args = parser.parse_args()

    if args.status:
        pending = get_pending_subphases(args.plan_path)
        total = len(parse_plan_xml(Path(args.plan_path)).findall('.//phase/phase'))
        completed = total - len(pending)
        print(f"Plan: {args.plan_path}")
        print(f"Progress: {completed}/{total} subphases completed")
        if pending:
            print(f"\nPending ({len(pending)}):")
            for p in pending:
                print(f"  - {p}")
        else:
            print("\nAll subphases completed!")
        return

    if args.test:
        prompts = [TEST_PROMPT]
    else:
        prompts = get_prompts(args.plan_path)

    await run_agent(
        prompts,
        args.plan_path,
        max_iterations=1 if args.test else args.max_iterations,
        model=args.model,
        resume_prompt_id=args.resume_prompt,
        resume_session_id=args.session,
    )


if __name__ == "__main__":
    asyncio.run(main())
