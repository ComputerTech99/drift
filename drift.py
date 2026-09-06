#!/usr/bin/env python3
"""drift: verify that requirements stated in an AI coding session landed in the code.

The invariant: the model reads English, the graph checks code, neither does the
other's job. Exactly one LLM call in this file (extract_requirements). Every
verification step after that is subprocess output, dict lookups, and regex.
"""
import argparse
import json
import os
import re
import subprocess
import sys

import requests

ASSERTION_TYPES = {
    "symbol_exists",
    "has_inbound_edge",
    "calls",
    "test_references",
    "literal_in_body",
}
STRUCTURAL_RELATIONS = {"DEFINES", "CONTAINS", "FILE_CHANGES_WITH"}

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
EXTRACTION_MODEL = "claude-haiku-4-5"

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

EXTRACTION_SYSTEM = """You turn a coding session's user requests into a checklist \
of verifiable requirements. You never see the code that was written and you never \
judge whether anything landed - a separate deterministic system does that.

For each distinct requirement you find, choose exactly one assertion type from this \
closed list and fill its arguments:

  symbol_exists(pattern)        a function/class/symbol matching a regex must exist
  has_inbound_edge(symbol)      something else in the code must reference this symbol
  calls(caller, callee)         `caller` must call `callee`
  test_references(symbol)       a test must reference this symbol
  literal_in_body(symbol, value) the literal `value` must appear in symbol's source

If a requirement does not cleanly fit one of these five, use {"type": "unverifiable"} \
instead of forcing a bad fit. Do not invent a sixth type."""

# JSON Schema for the requirement list. Passed as the Ollama `format` so the
# decoder is constrained to this shape - no prompt-only "please output JSON"
# and no post-hoc repair.
REQUIREMENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "requirement": {"type": "string"},
            "assertion": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": sorted(ASSERTION_TYPES | {"unverifiable"}),
                    },
                    "args": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "symbol": {"type": "string"},
                            "caller": {"type": "string"},
                            "callee": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["type", "args"],
                "additionalProperties": False,
            },
        },
        "required": ["requirement", "assertion"],
        "additionalProperties": False,
    },
}


def extract_requirements(user_prompts, backend="local"):
    """The one LLM call in this program. Sees only user-role prompt text -
    never assistant text, never tool_use payloads - so it extracts what was
    asked for, not a summary of what the agent did.

    Swappable backend, same return shape either way: a list of
    {"requirement": str, "assertion": {"type": str, "args": dict}}.
    Nothing downstream knows or cares which backend ran.
    """
    if backend == "local":
        items = _extract_local(user_prompts)
    elif backend == "api":
        items = _extract_api(user_prompts)
    else:
        raise SystemExit(f"unknown backend: {backend}")
    return _close_assertion_enum(items)


def _extract_local(user_prompts):
    """Ollama, local and offline. The requirement schema is enforced at
    decode time via `format`, so the response is already schema-valid JSON -
    no fence stripping, no repair.
    """
    response = requests.post(
        OLLAMA_CHAT_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {"role": "user", "content": user_prompts},
            ],
            "format": REQUIREMENTS_SCHEMA,
            "stream": False,
        },
        timeout=300,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"ollama returned malformed JSON despite a constraining schema: {exc}\n{content}"
        )


def _extract_api(user_prompts):
    """Anthropic Messages API, hosted fallback. Schema enforced at decode time
    via `output_config.format` (same no-repair contract as the local backend).
    The API's structured-output schema needs an object root, so the array is
    wrapped and unwrapped around the call - the returned shape is unaffected.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    wrapped_schema = {
        "type": "object",
        "properties": {"requirements": REQUIREMENTS_SCHEMA},
        "required": ["requirements"],
        "additionalProperties": False,
    }
    response = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        json={
            "model": EXTRACTION_MODEL,
            "max_tokens": 8192,
            "system": EXTRACTION_SYSTEM,
            "messages": [{"role": "user", "content": user_prompts}],
            "output_config": {"format": {"type": "json_schema", "schema": wrapped_schema}},
        },
        timeout=180,
    )
    response.raise_for_status()
    text = next(
        b.get("text", "") for b in response.json()["content"] if b.get("type") == "text"
    )
    try:
        return json.loads(text)["requirements"]
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"anthropic returned malformed JSON despite a constraining schema: {exc}\n{text}"
        )


def _close_assertion_enum(items):
    """Reject any assertion type outside the closed enum. The enum is closed -
    a backend cannot author a new one, however it got there."""
    requirements = []
    for item in items:
        assertion = item.get("assertion", {})
        atype = assertion.get("type")
        if atype not in ASSERTION_TYPES:
            assertion = {"type": "unverifiable"}
        requirements.append({"requirement": item.get("requirement", ""), "assertion": assertion})
    return requirements


def run_entire(args, repo):
    proc = subprocess.run(
        ["entire", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


# ---------------------------------------------------------------------------
# Step 1: transcript parsing
# ---------------------------------------------------------------------------

def parse_transcript(raw):
    """Parse checkpoint transcript JSONL into an ordered list of turns.

    The transcript is one JSONL line per content block, not per message: a
    single assistant message with a thinking block and a tool_use block is
    two lines that must collapse into one turn. Lines group into a turn when
    they share a message id (assistant replies carry a stable API message id
    across their streamed blocks); a line with no message id is its own turn
    (real user prompts and synthetic tool-result messages both arrive whole,
    one per line).

    Each turn: {index, role, kind, text, tool_uses: [{name, input}]}.
    `kind` is "prompt" (a real human message), "tool_result" (a synthetic
    user-role message carrying tool output), or "assistant". Block-level
    order within a turn is kept implicitly by list order, never exposed as
    a separate index — the only index that ever surfaces is the turn index.
    """
    turns = []
    current_key = object()
    current = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = record.get("role") or record.get("message", {}).get("role")
        if role not in ("user", "assistant"):
            continue
        message = record.get("message", record)
        content = message.get("content")
        msg_id = message.get("id")
        key = (role, msg_id) if msg_id else (role, id(record))

        if key != current_key:
            current = {
                "index": len(turns),
                "role": role,
                "kind": "assistant" if role == "assistant" else "prompt",
                "text_parts": [],
                "tool_uses": [],
            }
            turns.append(current)
            current_key = key

        if isinstance(content, str):
            current["text_parts"].append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    current["text_parts"].append(block.get("text", ""))
                elif btype == "tool_use":
                    current["tool_uses"].append(
                        {"name": block.get("name", ""), "input": block.get("input", {})}
                    )
                elif btype == "tool_result":
                    current["kind"] = "tool_result"

    for turn in turns:
        turn["text"] = "\n".join(turn.pop("text_parts"))
    return turns


# ---------------------------------------------------------------------------
# Step 3: graph index
# ---------------------------------------------------------------------------

def build_graph_index(raw):
    """Two indexes from `entire graph snapshot` ndjson: symbols by name, edges
    by source id. A third (id -> symbol) is kept purely to resolve relation
    endpoints back to names; it is not an independent verification index.
    """
    symbols_by_name = {}
    symbols_by_id = {}
    edges_by_source = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        rtype = record.get("record_type")
        if rtype == "symbol":
            name = record.get("name")
            symbols_by_name.setdefault(name, []).append(record)
            symbols_by_id[record.get("id")] = record
        elif rtype == "relation":
            edges_by_source.setdefault(record.get("from_id"), []).append(record)
    return {
        "by_name": symbols_by_name,
        "by_id": symbols_by_id,
        "edges_by_source": edges_by_source,
    }


def find_symbols(graph, pattern):
    """Symbol records whose name or qualified_name matches a regex pattern."""
    matches = []
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    for name, records in graph["by_name"].items():
        if rx.search(name or ""):
            matches.extend(records)
            continue
        for rec in records:
            if rx.search(rec.get("qualified_name") or ""):
                matches.append(rec)
    return matches


def all_edges(graph):
    for edges in graph["edges_by_source"].values():
        yield from edges


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--backend", choices=["local", "api"], default="local")
    args = parser.parse_args()

    transcript_raw = run_entire(
        ["checkpoint", "explain", args.checkpoint, "--transcript"], args.repo
    )
    turns = parse_transcript(transcript_raw)

    graph_raw = run_entire(
        ["graph", "snapshot", "--repo", ".", "--format", "ndjson"], args.repo
    )
    graph = build_graph_index(graph_raw)

    print(f"parsed {len(turns)} turns")
    print(f"parsed {len(graph['by_id'])} symbols")

    prompt_text = "\n\n".join(t["text"] for t in turns if t["kind"] == "prompt")
    requirements = extract_requirements(prompt_text, backend=args.backend)
    print(json.dumps(requirements, indent=2))


if __name__ == "__main__":
    main()
