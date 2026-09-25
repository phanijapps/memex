"""Parsers from harness-native session files to memex transcripts.

Every parser returns a ParsedTranscript: an optional SessionHeader
(extracted where the harness records session metadata) plus
conversational turns. Parsers are tolerant — unrecognized lines are
skipped and unparseable timestamps become "" (unknown).

The Codex parser is built against real rollout data: session_meta
(identity, cwd, git, provider, cli version), turn_context (model and
reasoning effort per turn), and token_usage_record payloads carrying
turn_token_usage / thread_token_usage. Session totals come from the
latest thread_token_usage; per-turn usage from the latest
turn_token_usage per turn — cumulative records are never summed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memex.domain.models import SessionHeader, TurnStreamEntry

HARNESSES: tuple[str, ...] = ("pi", "claude", "codex")

HEADER_TYPE = "memex_session_header"

_SESSION_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ParsedTranscript:
    """Parser output: header, turns, and session token totals.

    Token counts travel here (into the .meta.json sidecar), never in the
    transcript JSONL itself.
    """

    header: SessionHeader | None
    turns: list[TurnStreamEntry]
    session_usage: dict[str, int] | None = None


def normalize_ts(value: object) -> str:
    """Best-effort ISO8601-UTC normalization; "" when unknown."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError, OverflowError):
        pass
    return ""


def suggest_session_id(harness: str, path: Path) -> str:
    """Derive a charset-safe session id from the session file name."""
    raw = f"{harness}-{path.stem}"
    cleaned = _SESSION_ID_CHARS.sub("-", raw).strip("-.")
    return cleaned[:80] or f"{harness}-session"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _blocks_text(content: object) -> str:
    """Join text from a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if isinstance(part, str))
    return ""


def _opt_str(turn: dict[str, object], key: str) -> str | None:
    value = turn.get(key)
    return value if isinstance(value, str) and value else None


def _finalize(turns: list[dict[str, object]]) -> list[TurnStreamEntry]:
    entries: list[TurnStreamEntry] = []
    for number, turn in enumerate(turns, start=1):
        content = str(turn.get("content") or "")
        if not content.strip():
            continue
        entries.append(
            TurnStreamEntry(
                role=str(turn["role"]),
                content=content,
                turn=number,
                ts=str(turn.get("ts") or ""),
                tool_name=_opt_str(turn, "tool_name"),
                result=_opt_str(turn, "result"),
                query=_opt_str(turn, "query"),
                token_usage=turn.get("token_usage")  # type: ignore[arg-type]
                if isinstance(turn.get("token_usage"), dict)
                else None,
            )
        )
    return entries


# ---------------------------------------------------------------- pi


def _pi_usage(message: dict[str, object]) -> dict[str, int] | None:
    """Normalize pi's camelCase usage to the meta sidecar's snake_case."""
    usage = _usage_dict(message.get("usage"))
    if usage is None:
        return None
    renames = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cacheRead": "cache_read_tokens",
        "cacheWrite": "cache_write_tokens",
        "totalTokens": "total_tokens",
    }
    return {renames.get(key, key): value for key, value in usage.items()}


def parse_pi_session(path: Path) -> ParsedTranscript:
    """Parse a pi session JSONL (format v3) into turns.

    Message order follows file order, which is append order along the
    active branch. user/assistant text becomes conversation; toolResult
    and bashExecution become tool turns. Assistant messages carry a
    per-API-call ``usage`` object (input, output, cacheRead, cacheWrite,
    totalTokens); each is attached to the agent turn it billed and the
    session total is their sum, same contract as the other harnesses.
    """
    turns: list[dict[str, object]] = []
    totals: dict[str, int] = {}
    for entry in _read_jsonl(path):
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        ts = normalize_ts(message.get("timestamp"))
        if role == "user":
            turns.append(
                {"role": "user", "content": _blocks_text(message.get("content")), "ts": ts}
            )
        elif role == "assistant":
            usage = _pi_usage(message)
            if usage is not None:
                for key, value in usage.items():
                    totals[key] = totals.get(key, 0) + value
            turns.append(
                {
                    "role": "agent",
                    "content": _blocks_text(message.get("content")),
                    "ts": ts,
                    **({"token_usage": usage} if usage is not None else {}),
                }
            )
        elif role == "toolResult":
            turns.append(
                {
                    "role": "tool",
                    "content": str(message.get("toolName") or "tool"),
                    "tool_name": str(message.get("toolName") or "tool"),
                    "result": _blocks_text(message.get("content")),
                    "ts": ts,
                }
            )
        elif role == "bashExecution":
            command = str(message.get("command") or "")
            turns.append(
                {
                    "role": "tool",
                    "content": command,
                    "tool_name": "bash",
                    "result": str(message.get("output") or ""),
                    "query": command,
                    "ts": ts,
                }
            )
    return ParsedTranscript(header=None, turns=_finalize(turns), session_usage=totals or None)


# ---------------------------------------------------------------- claude


def parse_claude_transcript(path: Path) -> ParsedTranscript:
    """Parse a Claude Code transcript JSONL into header + turns.

    Assistant tool_use blocks become tool turns; user tool_result blocks
    become tool results. Non-conversational lines (mode, attachments,
    file snapshots) are skipped. Sidechain entries are excluded from
    turns and usage — they bill to their parent session context.

    Header extraction (real transcript shapes):
    - identity: sessionId / cwd / gitBranch / version appear on every
      line; identity comes from the first, freshness from the latest.
    - model + effort: message.model and the top-level effort field on
      assistant lines; ordered unique sets capture mid-session changes.
    - usage: message.usage is per-API-call (not cumulative like Codex
      thread usage), so the session total is the sum across assistant
      messages. Each call's usage is attached to the agent turn it
      billed, same contract as Codex turn_token_usage.
    """
    turns: list[dict[str, object]] = []
    started = ""
    ended = ""
    session_id = ""
    latest: dict[str, Any] | None = None
    models: list[str] = []
    efforts: list[str] = []
    totals: dict[str, int] = {}
    last_agent_index: int | None = None

    for entry in _read_jsonl(path):
        ts = normalize_ts(entry.get("timestamp"))
        if ts:
            ended = ts
            if not started:
                started = ts
        if isinstance(entry.get("sessionId"), str) and not session_id:
            session_id = entry["sessionId"]
        if any(k in entry for k in ("version", "cwd", "gitBranch")):
            latest = entry
        entry_type = entry.get("type")
        if entry.get("isSidechain"):
            continue
        if entry_type not in ("user", "assistant"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        ts = ts or normalize_ts(message.get("timestamp"))
        content = message.get("content")
        if entry_type == "assistant":
            model = message.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
            effort = entry.get("effort")
            if isinstance(effort, str) and effort and effort not in efforts:
                efforts.append(effort)
            usage = _usage_dict(message.get("usage"))
            if usage is not None:
                for key, value in usage.items():
                    totals[key] = totals.get(key, 0) + value
            blocks = content if isinstance(content, list) else []
            text = _blocks_text(blocks)
            if text.strip():
                turns.append({"role": "agent", "content": text, "ts": ts})
                last_agent_index = len(turns) - 1
                if usage is not None:
                    turns[last_agent_index]["token_usage"] = usage
            elif usage is not None and last_agent_index is not None:
                # tool-only response: bill usage to the previous agent turn
                turns[last_agent_index]["token_usage"] = usage
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                turns.append(
                    {
                        "role": "tool",
                        "content": str(block.get("name") or "tool"),
                        "tool_name": str(block.get("name") or "tool"),
                        "query": json.dumps(block.get("input", {}), default=str),
                        "ts": ts,
                    }
                )
        else:
            blocks = content if isinstance(content, list) else []
            text = _blocks_text(content)
            if text.strip():
                turns.append({"role": "user", "content": text, "ts": ts})
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                turns.append(
                    {
                        "role": "tool",
                        "content": "tool_result",
                        "result": _blocks_text(block.get("content")),
                        "ts": ts,
                    }
                )
    header_meta: dict[str, object] = {}
    if latest is not None:
        header_meta = {
            "cli_version": latest.get("version"),
            "cwd": latest.get("cwd"),
            "git_branch": latest.get("gitBranch"),
            "entrypoint": latest.get("entrypoint"),
        }
    if models:
        header_meta["models"] = models
    if efforts:
        header_meta["reasoning_efforts"] = efforts
    header_meta = {k: v for k, v in header_meta.items() if v is not None}

    duration_s: float | None = None
    start_epoch = _parse_iso_epoch(started)
    end_epoch = _parse_iso_epoch(ended)
    if start_epoch is not None and end_epoch is not None and end_epoch >= start_epoch:
        duration_s = round(end_epoch - start_epoch, 3)

    header = SessionHeader(
        harness="claude",
        session_id=session_id or suggest_session_id("claude", path),
        started_at=started or None,
        ended_at=ended or None,
        duration_s=duration_s,
        meta=header_meta,
    )
    return ParsedTranscript(header=header, turns=_finalize(turns), session_usage=totals or None)


# ---------------------------------------------------------------- codex


def _usage_dict(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, int):
            usage[str(key)] = item
    return usage or None


def _parse_iso_epoch(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_codex_rollout(path: Path) -> ParsedTranscript:
    """Parse a Codex rollout JSONL into header + turns (tolerant).

    Tool traffic becomes tool turns: function_call / custom_tool_call
    entries carry the invocation (query), and their paired outputs
    (matched by call_id) carry the result. agent_message entries are
    assistant text. compacted entries are skipped: rollouts are
    append-only, so the real pre-compaction turns are already in the
    file and the conversation continues after the compact marker.
    Header extraction (real rollout shapes):
    - session_meta: session id, cwd, git (branch/commit/repo), provider,
      cli version, start timestamp. Resumed sessions re-emit session_meta;
      identity comes from the first, cwd/git refresh from the latest.
    - turn_context: model and reasoning effort per turn; ordered unique
      sets capture mid-session model changes.
    - token_usage_record: the latest thread_token_usage is the session
      total (never summed); the latest turn_token_usage per turn is
      attached to the agent turn it billed (records follow the response
      they describe in file order).
    """
    turns: list[dict[str, object]] = []
    meta: dict[str, object] = {}
    first_meta: dict[str, Any] | None = None
    latest_meta: dict[str, Any] | None = None
    started = ""
    ended = ""
    thread_usage: dict[str, int] | None = None
    last_agent_index: int | None = None
    models: list[str] = []
    efforts: list[str] = []
    pending_calls: dict[str, str] = {}

    for entry in _read_jsonl(path):
        ts = normalize_ts(entry.get("timestamp"))
        if ts:
            ended = ts
        entry_type = entry.get("type")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None

        if entry_type == "session_meta" and payload is not None:
            if first_meta is None:
                first_meta = payload
                started = normalize_ts(payload.get("timestamp")) or ts
            latest_meta = payload
            continue
        if entry_type == "turn_context" and payload is not None:
            model = payload.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
            settings = payload.get("collaboration_mode")
            effort = None
            if isinstance(settings, dict):
                inner = settings.get("settings")
                if isinstance(inner, dict) and isinstance(inner.get("reasoning_effort"), str):
                    effort = inner["reasoning_effort"]
            if effort is None and isinstance(payload.get("reasoning_effort"), str):
                effort = payload["reasoning_effort"]
            if effort and effort not in efforts:
                efforts.append(effort)
            continue
        if entry_type == "token_usage_record" and payload is not None:
            thread = _usage_dict(payload.get("thread_token_usage"))
            if thread is not None:
                thread_usage = thread  # latest wins; cumulative never summed
            turn_usage = _usage_dict(payload.get("turn_token_usage"))
            if turn_usage is not None and last_agent_index is not None:
                turns[last_agent_index]["token_usage"] = turn_usage
            continue
        if entry_type == "compacted":
            continue  # append-only file already holds the real earlier turns
        if entry_type != "response_item" or payload is None:
            continue

        kind = payload.get("type")

        if kind == "message":
            role = payload.get("role")
            content = payload.get("content")
            texts: list[str] = []
            if isinstance(content, list):
                texts = [
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") in ("input_text", "output_text", "text")
                ]
            elif isinstance(content, str):
                texts = [content]
            text = "\n".join(part for part in texts if part)
            if not text.strip():
                continue
            if role == "user":
                turns.append({"role": "user", "content": text, "ts": ts})
            elif role in ("assistant", "agent"):
                turns.append({"role": "agent", "content": text, "ts": ts})
                last_agent_index = len(turns) - 1
        elif kind == "agent_message":
            text = str(payload.get("content") or payload.get("text") or "")
            if text.strip():
                turns.append({"role": "agent", "content": text, "ts": ts})
                last_agent_index = len(turns) - 1
        elif kind in ("function_call", "custom_tool_call", "web_search_call", "tool_search_call"):
            name = str(payload.get("name") or payload.get("action") or kind)
            invocation: str | None = None
            for key in ("arguments", "input", "query"):
                value = payload.get(key)
                if isinstance(value, str):
                    invocation = value
                    break
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                pending_calls[call_id] = name
            turns.append(
                {
                    "role": "tool",
                    "content": name,
                    "tool_name": name,
                    "query": invocation if isinstance(invocation, str) else None,
                    "ts": ts,
                }
            )
        elif kind in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            call_id = payload.get("call_id")
            output = payload.get("output")
            result = output if isinstance(output, str) else json.dumps(output, default=str)
            out_name: str | None = (
                pending_calls.get(str(call_id)) if isinstance(call_id, str) else None
            )
            turns.append(
                {
                    "role": "tool",
                    "content": out_name or "tool_output",
                    "tool_name": out_name,
                    "result": result,
                    "ts": ts,
                }
            )

    if first_meta is not None:
        source = latest_meta if latest_meta is not None else first_meta
        git = source.get("git")
        meta = {
            "cli_version": source.get("cli_version"),
            "provider": source.get("model_provider"),
            "cwd": source.get("cwd"),
            "originator": source.get("originator"),
            "git": git if isinstance(git, dict) else None,
            "models": models,
            "reasoning_efforts": efforts,
        }
        if latest_meta is not None and latest_meta is not first_meta:
            meta["resumed"] = True
        meta = {key: value for key, value in meta.items() if value is not None}

    session_id = ""
    if first_meta is not None:
        raw_id = first_meta.get("session_id") or first_meta.get("id")
        if isinstance(raw_id, str):
            session_id = raw_id

    duration_s: float | None = None
    start_epoch = _parse_iso_epoch(started)
    end_epoch = _parse_iso_epoch(ended)
    if start_epoch is not None and end_epoch is not None and end_epoch >= start_epoch:
        duration_s = round(end_epoch - start_epoch, 3)

    header = SessionHeader(
        harness="codex",
        session_id=session_id or suggest_session_id("codex", path),
        started_at=started or None,
        ended_at=ended or None,
        duration_s=duration_s,
        meta=meta,
    )
    return ParsedTranscript(header=header, turns=_finalize(turns), session_usage=thread_usage)


PARSERS: dict[str, Callable[[Path], ParsedTranscript]] = {
    "pi": parse_pi_session,
    "claude": parse_claude_transcript,
    "codex": parse_codex_rollout,
}


def parse_transcript(harness: str, path: Path) -> ParsedTranscript:
    try:
        parser = PARSERS[harness]
    except KeyError:
        raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESSES}") from None
    return parser(path)


def read_transcript_turns(path: Path) -> list[TurnStreamEntry]:
    """Read a memex transcript JSONL: header line(s) are skipped, older
    turn-only files read unchanged."""
    turns: list[TurnStreamEntry] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or "role" not in entry:
            continue  # session header or foreign line
        try:
            turns.append(TurnStreamEntry.from_dict(entry))
        except ValueError:
            continue
    return turns
