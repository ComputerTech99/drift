#!/usr/bin/env python3
"""drift: verify that requirements stated in an AI coding session landed in the code.
Invariant: the model reads English (one call, in extract_requirements); the graph
checks code. Everything after extraction is subprocess output, dict lookups, and
regex - no model call in the verification path."""
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
# --backend api is opt-in only (--allow-external, see main()), never the default -
# Anthropic arguably isn't a *new* party for a transcript Claude Code itself wrote,
# but this tool does not rely on that argument by default.
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
EXTRACTION_MODEL = "claude-haiku-4-5"

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

EXTRACTION_SYSTEM = """You turn a coding session's user requests into a checklist \
of verifiable requirements. You never see the code that was written and you never \
judge whether anything landed - a separate deterministic system does that.

Step 1: find requirements. Extract only things the user asked to be built or \
changed. Skip any sentence that describes something that already exists, gives \
background, or explains rationale - it is context, not a requirement, even if it \
sits right next to real requirements in the same message. Examples of what to \
SKIP: "the entire CLI captures agent sessions as Checkpoints stored in Git" \
(describes an existing tool), "a diff shows what the code is, never what is \
missing" (rationale for why the tool exists), "entire graph snapshot emits a \
deterministic graph" (describes an existing command's behavior). Examples of what \
to KEEP: "build a CLI tool called drift", "add retry with exponential backoff", \
"the tool must exit non-zero on failure".

Step 2: for each requirement you kept, choose exactly one assertion type from this \
closed list and fill its arguments, OR decide it is unverifiable:

  symbol_exists(pattern)        a function/class/symbol matching a regex must exist
  has_inbound_edge(symbol)      something else in the code must reference this symbol
  calls(caller, callee)         `caller` must call `callee`
  test_references(symbol)       a test must reference this symbol
  literal_in_body(symbol, value) the literal `value` must appear in symbol's source

A "symbol" is a function, class, or method that tree-sitter parses out of source as its own named definition - never a filename, a line count, or a parameter, instance attribute, local variable, or config default, even though those are also "variables" in the everyday sense. `timeout` in `def __init__(self, timeout=5.0)` is real code but not independently checkable here: unverifiable, not symbol_exists("timeout"). "single file drift.py" is not symbol_exists("drift.py") either: a file is not a symbol. Likewise unverifiable: counts ("retry up to 5 times", "at most 400 lines", "exactly one call to the API" - a count, not a reachability claim even though "call" is right there); file layout and line-count targets; dependency/tooling constraints ("stdlib plus requests only"); performance, timing, or parameter/attribute values ("must be fast", "add a timeout", "default retry count is 5"); and process constraints ("do not reimplement entire"). Before you emit any \
assertion other than unverifiable, name the exact symbol it is about - if you \
cannot name one that tree-sitter would parse, it is unverifiable. When in doubt, \
use unverifiable - a forced assertion that can never truthfully pass is worse \
than an admitted gap. Do not invent a sixth type."""

# Passed as the Ollama `format` / Anthropic `output_config.format` so the decoder
# is constrained to this shape at decode time - no fence stripping, no repair.
_ARG_KEYS = ("pattern", "symbol", "caller", "callee", "value")
_ARGS_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in _ARG_KEYS}, "additionalProperties": False}
_ASSERTION_SCHEMA = {
    "type": "object",
    "properties": {"type": {"type": "string", "enum": sorted(ASSERTION_TYPES | {"unverifiable"})}, "args": _ARGS_SCHEMA},
    "required": ["type", "args"], "additionalProperties": False,
}
REQUIREMENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"requirement": {"type": "string"}, "assertion": _ASSERTION_SCHEMA},
        "required": ["requirement", "assertion"], "additionalProperties": False,
    },
}

def extract_requirements(user_prompts, backend="local"):
    """The one LLM call in this program. `user_prompts` is user-role prompt text only - never assistant text, never tool_use payloads - so this extracts what was asked for, not a summary of what the agent did. Swappable backend, same return shape either way; nothing downstream knows or cares which ran."""
    if backend == "local":
        items = _extract_local(user_prompts)
    elif backend == "api":
        items = _extract_api(user_prompts)
    else:
        raise SystemExit(f"unknown backend: {backend}")
    return _close_assertion_enum(items)

def _extract_local(user_prompts):
    messages = [{"role": "system", "content": EXTRACTION_SYSTEM}, {"role": "user", "content": user_prompts}]
    response = requests.post(
        OLLAMA_CHAT_URL,
        json={"model": OLLAMA_MODEL, "messages": messages, "format": REQUIREMENTS_SCHEMA, "stream": False},
        timeout=300,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ollama returned malformed JSON despite a constraining schema: {exc}\n{content}")

def _extract_api(user_prompts):
    """Anthropic Messages API, hosted fallback. The API's structured-output schema needs an object root, so the array is wrapped/unwrapped around the call; the returned shape is unaffected."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    wrapped_schema = {"type": "object", "properties": {"requirements": REQUIREMENTS_SCHEMA}, "required": ["requirements"], "additionalProperties": False}
    headers = {"content-type": "application/json", "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    body = {
        "model": EXTRACTION_MODEL,
        "max_tokens": 8192,
        "system": EXTRACTION_SYSTEM,
        "messages": [{"role": "user", "content": user_prompts}],
        "output_config": {"format": {"type": "json_schema", "schema": wrapped_schema}},
    }
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=180)
    response.raise_for_status()
    text = next(b.get("text", "") for b in response.json()["content"] if b.get("type") == "text")
    try:
        return json.loads(text)["requirements"]
    except json.JSONDecodeError as exc:
        raise SystemExit(f"anthropic returned malformed JSON despite a constraining schema: {exc}\n{text}")

def _close_assertion_enum(items):
    """Reject any assertion type outside the closed enum, however it got there - a backend cannot author a new one."""
    requirements = []
    for item in items:
        assertion = item.get("assertion", {})
        if assertion.get("type") not in ASSERTION_TYPES:
            assertion = {"type": "unverifiable"}
        requirements.append({"requirement": item.get("requirement", ""), "assertion": assertion})
    return requirements

def run_entire(args, repo):
    proc = subprocess.run(["entire", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout

# Step 1: transcript parsing
def parse_transcript(raw):
    """Group transcript JSONL lines into turns by message boundary, not by line: it's one line per content block, so a streamed assistant message (thinking + tool_use) is multiple lines sharing one API message id that must collapse into one turn. A line with no message id (a real user prompt, or a synthetic tool-result message) is already whole and is its own turn. Turn: {index, role, kind, text, tool_uses}; kind is "prompt", "tool_result", or "assistant". Only the turn index ever surfaces downstream - no block-level position leaks out."""
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
        if not isinstance(record, dict):
            continue
        # `or {}` not a .get default: redaction may null a present key, not omit it.
        role = record.get("role") or (record.get("message") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        message = record.get("message") or record
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
                    current["tool_uses"].append({"name": block.get("name", ""), "input": block.get("input") or {}})
                elif btype == "tool_result":
                    current["kind"] = "tool_result"

    for turn in turns:
        turn["text"] = "\n".join(turn.pop("text_parts"))
    return turns

# Step 3: graph index
def build_graph_index(raw):
    """Two indexes from `entire graph snapshot` ndjson: symbols by name, edges by source id. `by_id` is kept alongside purely to resolve relation endpoints back to symbol records - an implementation detail of the same index, not a third independent one."""
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
        if not isinstance(record, dict):
            continue
        rtype = record.get("record_type")
        if rtype == "symbol":
            symbols_by_name.setdefault(record.get("name"), []).append(record)
            symbols_by_id[record.get("id")] = record
        elif rtype == "relation":
            edges_by_source.setdefault(record.get("from_id"), []).append(record)
    return {"by_name": symbols_by_name, "by_id": symbols_by_id, "edges_by_source": edges_by_source}

def _compile(pattern):
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))

def find_symbols(graph, pattern):
    """Symbol records whose name or qualified_name matches a regex SEARCH (substring position, not full-string equality) - "run" matches "run_entire"."""
    rx = _compile(pattern)
    matches = []
    for name, records in graph["by_name"].items():
        if rx.search(name or ""):
            matches.extend(records)
            continue
        matches.extend(rec for rec in records if rx.search(rec.get("qualified_name") or ""))
    return matches

def all_edges(graph):
    for edges in graph["edges_by_source"].values():
        yield from edges

def _resolve_symbols(graph, name):
    """Exact name match first; only fall back to find_symbols's regex/substring search if nothing matches exactly, so an unambiguous name doesn't get diluted by unrelated substring hits."""
    exact = graph["by_name"].get(name)
    return exact if exact else (find_symbols(graph, name) if name else [])

# Step 4: assertion evaluators
CALL_RELATION_TYPES = {"CALLS", "ASYNC_CALLS"}  # relation_set distinguishes calls - not approximated

# Heuristic for "looks like a test": a test(s)/__tests__ path segment, or a
# filename ending in _test.<ext> - the only definition of "test" on offer.
TEST_PATH_PATTERN = re.compile(r"(^|/)(tests?|__tests__)(/|$)|_test\.[^/.]+$", re.IGNORECASE)

def eval_symbol_exists(args, graph):
    """Never None - existence is always answerable."""
    return bool(find_symbols(graph, args.get("pattern", "")))

def eval_has_inbound_edge(args, graph):
    """Any relation, of any type, from any source, landing on this symbol. False (not None) when the symbol doesn't exist: nonexistence definitively means no inbound edge."""
    ids = {s["id"] for s in _resolve_symbols(graph, args.get("symbol", ""))}
    return bool(ids) and any(edge.get("to_id") in ids for edge in all_edges(graph))

def eval_calls(args, graph):
    caller_ids = [s["id"] for s in _resolve_symbols(graph, args.get("caller", ""))]
    callee_ids = {s["id"] for s in _resolve_symbols(graph, args.get("callee", ""))}
    if not caller_ids or not callee_ids:
        return False
    return any(
        edge.get("type") in CALL_RELATION_TYPES and edge.get("to_id") in callee_ids
        for cid in caller_ids
        for edge in graph["edges_by_source"].get(cid, [])
    )

def eval_test_references(args, graph):
    ids = {s["id"] for s in _resolve_symbols(graph, args.get("symbol", ""))}
    if not ids:
        return False
    for edge in all_edges(graph):
        if edge.get("to_id") in ids:
            source = graph["by_id"].get(edge.get("from_id"))
            if source and TEST_PATH_PATTERN.search(source.get("file_path") or ""):
                return True
    return False

def eval_literal_in_body(args, graph):
    """Regex search for `value` within the symbol's source range. Falls back to the whole file when the snapshot has no start_line/end_line - the evidence string names this as a widened window."""
    value = args.get("value", "")
    if not value:
        return None
    candidates = _resolve_symbols(graph, args.get("symbol", ""))
    if not candidates:
        return False
    repo = graph["repo"]
    for rec in candidates:
        path = rec.get("file_path")
        if not path:
            continue
        try:
            lines = open(os.path.join(repo, path), encoding="utf-8", errors="replace").readlines()
        except OSError:
            continue
        start, end = rec.get("start_line"), rec.get("end_line")
        snippet = "".join(lines[start - 1 : end]) if start and end else "".join(lines)
        if re.search(re.escape(value), snippet):
            return True
    return False

# Per type: (evaluator, evidence-string builder, needles-for-drop-scan builder,
# evidence_class - set by what kind of check the evaluator does, not by whether
# it passed: a graph relation lookup is confirmed_structural; a regex-over-text
# or file-path heuristic is heuristic, however confident its evidence reads).
ASSERTION_HANDLERS = {
    "symbol_exists": (eval_symbol_exists, lambda a, ok: f"regex search for /{a.get('pattern')}/ in symbol names: {'match' if ok else 'no match'}", lambda a: [a.get("pattern")], "confirmed_structural"),
    "has_inbound_edge": (eval_has_inbound_edge, lambda a, ok: f"inbound relations to '{a.get('symbol')}': {'found' if ok else 'not found'}", lambda a: [a.get("symbol")], "confirmed_structural"),
    "calls": (eval_calls, lambda a, ok: f"CALLS/ASYNC_CALLS from '{a.get('caller')}' to '{a.get('callee')}': {'found' if ok else 'not found'}", lambda a: [a.get("caller"), a.get("callee")], "confirmed_structural"),
    "test_references": (eval_test_references, lambda a, ok: f"inbound edge from a test-path file to '{a.get('symbol')}': {'found' if ok else 'not found'}", lambda a: [a.get("symbol")], "heuristic"),
    "literal_in_body": (eval_literal_in_body, lambda a, ok: f"regex search for {a.get('value')!r} in '{a.get('symbol')}' source: {'found' if ok else 'not found'}", lambda a: [a.get("value")], "heuristic"),
}

def find_dropped_turn(turns, needles):
    """Scan Write/Edit tool_use payloads BACKWARDS for the requirement's
    symbol/literal; a hit becomes `dropped`, attributed to the last turn it
    appeared in - textual, so an unrelated turn reusing the same string can
    misattribute (the open risk noted in the design). None if it never
    appeared in a write or edit."""
    patterns = [_compile(n) for n in needles if n]
    if not patterns:
        return None
    for turn in reversed(turns):
        for tool_use in turn["tool_uses"]:
            if tool_use["name"] not in ("Write", "Edit"):
                continue
            inp = tool_use["input"]
            text = inp.get("content", "") + inp.get("old_string", "") + inp.get("new_string", "")
            if any(p.search(text) for p in patterns):
                return turn["index"]
    return None

# main
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--backend", choices=["local", "api"], default="local")
    parser.add_argument("--allow-external", action="store_true",
        help="required with --backend api: sends user-prompt text off this machine")
    args = parser.parse_args()
    if args.backend == "api" and not args.allow_external:
        raise SystemExit("--backend api also requires --allow-external: it sends user-prompt "
            "text to the Anthropic API, off this machine. --backend local (Ollama, on "
            "localhost) is the default and sends nothing off-machine.")
    if args.backend == "api":
        print(f"warning: sending user-prompt text to {ANTHROPIC_API_URL} ({EXTRACTION_MODEL})", file=sys.stderr)

    transcript_raw = run_entire(["checkpoint", "explain", args.checkpoint, "--transcript"], args.repo)
    turns = parse_transcript(transcript_raw)

    graph_raw = run_entire(["graph", "snapshot", "--repo", ".", "--format", "ndjson"], args.repo)
    graph = build_graph_index(graph_raw)
    graph["repo"] = args.repo

    print(f"parsed {len(turns)} turns")
    print(f"parsed {len(graph['by_id'])} symbols")

    prompt_text = "\n\n".join(t["text"] for t in turns if t["kind"] == "prompt")
    requirements = extract_requirements(prompt_text, backend=args.backend)

    rows = []
    for req in requirements:
        req_args = req["assertion"].get("args", {})
        handler = ASSERTION_HANDLERS.get(req["assertion"]["type"])
        result = handler[0](req_args, graph) if handler else None
        if result is None:
            # unverifiable type, or nothing to check - a real verdict, not a row to drop.
            rows.append((req["requirement"], "unverified", "no checkable assertion", "requires_verification"))
            continue
        verdict = "landed" if result else "absent"
        evidence = handler[1](req_args, result)
        evidence_class = handler[3]
        if verdict == "absent":
            dropped_at = find_dropped_turn(turns, handler[2](req_args))
            if dropped_at is not None:
                verdict = "dropped"
                evidence += f"; appeared in a write/edit at turn {dropped_at}, missing from final graph"
                evidence_class = "heuristic"  # textual turn attribution, however the base check tiers
        rows.append((req["requirement"], verdict, evidence, evidence_class))

    label_width = min(60, max((len(r[0]) for r in rows), default=10))
    header = f"{'requirement':<{label_width}}  {'verdict':<10}  {'evidence_class':<20}  evidence"
    print(f"\n{header}")
    for requirement, verdict, evidence, evidence_class in rows:
        label = requirement if len(requirement) <= label_width else requirement[: label_width - 1] + "…"
        print(f"{label:<{label_width}}  {verdict:<10}  {evidence_class:<20}  {evidence}")
    deferred = sum(1 for _, verdict, _, _ in rows if verdict == "unverified")
    print(f"\n({deferred} requirement(s) unverified: unverifiable type or no answer)")

    if any(verdict in ("absent", "dropped") for _, verdict, _, _ in rows):
        sys.exit(1)

if __name__ == "__main__":
    main()
