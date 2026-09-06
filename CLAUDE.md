# drift

Verifies whether the requirements stated in an AI coding session actually landed
in the code that shipped. Reads an Entire Checkpoint, extracts what was asked
for, checks each item against a deterministic code graph.

A diff shows what the code is. It cannot show what is missing. This tool names
the missing things.

## The invariant

**The model reads English. The graph checks code. Neither does the other's job.**

There is exactly one LLM call in this program: turning a multi-turn transcript
into a discrete requirement list. That is a reading-comprehension task.

Everything after that step is deterministic — subprocess calls, dict lookups,
regex. No model call appears anywhere in the verification path.

If you are about to add a model call to check, judge, score, or summarise
something, stop. Asking a model to eyeball a diff and confirm requirements were
met produces confident agreement, which is the exact failure this tool exists to
catch. Reproducing it inside the tool defeats the entire project.

## The assertion enum is closed

Five types. Not six.

```
symbol_exists(pattern)
has_inbound_edge(symbol)
calls(caller, callee)
test_references(symbol)
literal_in_body(symbol, value)
```

The extraction model selects from this list and fills arguments. It cannot
author new ones. Anything that does not fit returns `unverifiable`, which
becomes the `unverified` verdict.

`unverified` is a feature. A tool that admits what it cannot check is more
trustworthy than one that guesses. Do not add a `semantic_match` type, a
similarity score, or an LLM fallback to reduce the count of `unverified` rows.
The honest gap is the point.

## Verdicts

| Verdict | Meaning |
|---|---|
| `landed` | assertion passes |
| `partial` | symbol exists, `has_inbound_edge` fails |
| `absent` | assertion fails, no trace in any turn |
| `dropped` | appeared in a `tool_use` payload, gone from the final graph |
| `unverified` | no assertion type fits |

`dropped` carries the turn index where the symbol last appeared. Every verdict
carries `evidence`, currently always `"tool_calls"`. Keep the field.

## Out of scope

Do not build these. They are exclusions, not gaps.

- **No auto-fixing.** Reports a missing requirement, never writes it. A wrong
  repair is worse than an accurate complaint.
- **No scores or grades.** Per-requirement verdicts are actionable. A percentage
  is not.
- **No natural-language judgement in verification.** See the invariant.
- No caching, plugin system, config file, or web UI.

## Commands

```bash
# run it
python drift.py --repo ../demo-repo --checkpoint <id>

# the two subprocesses it wraps, both with cwd=--repo
entire checkpoint explain <id> --transcript
entire graph snapshot --repo . --format ndjson

# useful while debugging
entire checkpoint explain --json           # list checkpoints
entire graph capabilities --json           # language coverage
```

Requires `entire` 0.6.2+. Below that the `--transcript` and `--json` export
flags do not exist.

## Conventions

- Python 3, stdlib plus `requests`. No other dependencies.
- Single file, `drift.py`. Target 250 lines, hard ceiling 400.
- Shell out to `entire` via `subprocess`. Never reimplement it.
- Model: `claude-sonnet-5`, one call, strict JSON out, no prose or fences.
- Parse the real ndjson field names. Do not assume a schema.
- Exit non-zero if any verdict is `absent` or `dropped`.

## Working style

Build in stages and stop for review between them. Do not scaffold the full
verdict set before a single transcript has been parsed successfully.

Stage order: transcript + graph parsing → extraction → `landed`/`absent` →
`dropped` → `partial`/`unverified`.

## Note

This repo has `entire enable` active. The session building this tool is itself a
Checkpoint, and a valid input to the tool. If you drop a requirement while
refactoring, `drift` will eventually find it.
