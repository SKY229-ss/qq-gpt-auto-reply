"""Text generation through the official, already-authenticated Codex CLI."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal

DISABLED = (
    "shell_tool", "unified_exec", "apps", "plugins", "remote_plugin", "hooks",
    "multi_agent", "browser_use", "browser_use_external", "computer_use",
    "in_app_browser", "image_generation", "view_image", "goals", "sleep_tool",
    "workspace_dependencies", "tool_suggest", "skill_search",
    "skill_mcp_dependency_install", "code_mode_host",
)


class GPTBridge:
    def __init__(self, cwd: Path):
        self.cwd = cwd.resolve()
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.executable = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex"
        self.lock = asyncio.Lock()

    def command(self):
        args = [self.executable, "exec", "--ignore-user-config", "--skip-git-repo-check",
                "--ephemeral", "--sandbox", "read-only", "--cd", str(self.cwd)]
        for feature in DISABLED:
            args += ["--disable", feature]
        args += ["--enable", "skip_host_skill_discovery"]
        for setting in ('web_search="disabled"', 'project_doc_max_bytes=0',
                        'mcp_servers={}', 'approval_policy="never"'):
            args += ["-c", setting]
        return args + ["--json", "-"]

    async def generate(self, text: str) -> str:
        # Strip inherited task/connector context. The official CLI handles its
        # own saved authentication; this application never reads auth tokens.
        allowed = {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME",
                   "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE"}
        env = {k: v for k, v in os.environ.items() if k in allowed}
        instructions = Path(__file__).with_name("reply-instructions.txt").read_text()
        prompt = instructions + "\n\nCurrent QQ message (untrusted data):\n" + json.dumps(text[:12000], ensure_ascii=False)
        async with self.lock:
            proc = await asyncio.create_subprocess_exec(
                *self.command(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=env, start_new_session=True,
            )
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), 100)
            except BaseException:
                if proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), 3)
                    except asyncio.TimeoutError:
                        os.killpg(proc.pid, signal.SIGKILL)
                        await proc.wait()
                raise
            if proc.returncode != 0 or len(raw) > 1_000_000:
                raise RuntimeError("GPT connection failed; check Codex sign-in and usage")
            answer, complete = "", False
            for line in raw.decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.failed":
                    raise RuntimeError("GPT could not complete this reply")
                if event.get("type") == "turn.completed":
                    complete = True
                if event.get("type") in ("item.started", "item.completed"):
                    item = event.get("item", {})
                    if item.get("type") not in ("agent_message", "reasoning", "error"):
                        raise RuntimeError("Unexpected non-text activity; reply withheld")
                    if item.get("type") == "agent_message":
                        answer = item.get("text", "").strip()
            if not complete or not answer:
                raise RuntimeError("GPT returned no reply")
            return answer[:1800]
