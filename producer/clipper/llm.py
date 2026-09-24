"""Structured-output calls to Claude.

Backends:
  claude_cli - `claude -p --json-schema` using the local Claude Code login (no API key)
  api        - Anthropic SDK `messages.parse` (needs ANTHROPIC_API_KEY)
"""
import json
import os
import shutil
import subprocess
from typing import TypeVar

from pydantic import BaseModel

from .config import cfg

T = TypeVar("T", bound=BaseModel)

# Backup when the Claude plan's usage limit is hit: the same CLI call is retried on this pay-per-use
# API key, and the rest of the run stays on it. Kept under a different name so the CLI doesn't
# prefer it over the subscription while the plan still has room.
BACKUP_KEY = os.environ.get("ANTHROPIC_API_KEY_BACKUP")
_on_backup = False


def _limit_hit(text: str) -> bool:
    t = text.lower()
    return "limit" in t and any(w in t for w in ("usage", "reached", "hit your", "exceeded", "rate"))


def ask(prompt: str, schema: type[T], system: str = "", files: list[str] | None = None) -> T:
    """files: local image/document paths Claude should look at (vision, via the CLI's Read tool)."""
    backend = cfg()["llm"]["backend"]
    if backend == "api" and not files:
        return _ask_api(prompt, schema, system)
    return _ask_cli(prompt, schema, system, files)


def _ask_api(prompt: str, schema: type[T], system: str) -> T:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=cfg()["llm"]["model"],
        max_tokens=16000,
        system=system or anthropic.NOT_GIVEN,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Claude returned no structured output (stop_reason={response.stop_reason})")
    return response.parsed_output


def _claude_exe() -> str:
    """Real claude binary. The npm claude.cmd shim goes through cmd.exe, which mangles '|' etc. in arguments."""
    from pathlib import Path

    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    if exe.lower().endswith(".cmd"):
        real = Path(exe).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if real.exists():
            return str(real)
    return exe


def _ask_cli(prompt: str, schema: type[T], system: str, files: list[str] | None = None) -> T:
    full = f"{system}\n\n{prompt}" if system else prompt
    if files:
        listing = "\n".join(f"- {f}" for f in files)
        full = f"{full}\n\nFirst read these files with the Read tool, then answer:\n{listing}"
    exe = _claude_exe()
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--model", cfg()["llm"]["model"],
    ]
    cmd += (["--allowedTools", "Read", "--permission-mode", "acceptEdits"] if files
            else ["--tools", ""])  # pure reasoning; no tool use
    global _on_backup
    for attempt in range(2):
        env = None
        if _on_backup:
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
            env["ANTHROPIC_API_KEY"] = BACKUP_KEY
        proc = subprocess.run(cmd, input=full, capture_output=True, text=True, encoding="utf-8",
                              shell=False, env=env)
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = {"is_error": True, "result": proc.stderr.strip() or proc.stdout[:500]}
        failed = proc.returncode != 0 or envelope.get("is_error")
        if failed and not _on_backup and BACKUP_KEY and _limit_hit(str(envelope.get("result"))):
            print("[llm] Claude plan limit reached - switching to the backup API key for the rest of this run")
            _on_backup = True
            continue
        break
    if failed:
        raise RuntimeError(f"claude -p error: {envelope.get('result')}  (fix login with: claude auth login)")
    payload = envelope.get("structured_output") or envelope.get("result")
    if isinstance(payload, str):
        payload = json.loads(payload)
    return schema.model_validate(payload)
