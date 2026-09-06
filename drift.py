#!/usr/bin/env python3
"""drift: verify that requirements stated in an AI coding session landed in the code.

The invariant: the model reads English, the graph checks code, neither does the
other's job. Exactly one LLM call in this file (extract_requirements). Every
verification step after that is subprocess output, dict lookups, and regex.
"""
import argparse
import json
import re
import subprocess
import sys

ASSERTION_TYPES = {
    "symbol_exists",
    "has_inbound_edge",
    "calls",
    "test_references",
    "literal_in_body",
}
STRUCTURAL_RELATIONS = {"DEFINES", "CONTAINS", "FILE_CHANGES_WITH"}


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


if __name__ == "__main__":
    main()
