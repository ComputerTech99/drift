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
carries an `evidence` string describing the specific check that ran.

Every verdict also carries `evidence_class`: `confirmed_structural` for a
direct graph relation lookup (`symbol_exists`, `has_inbound_edge`, `calls`),
`heuristic` for anything that reads source text or a file path to infer intent
(`literal_in_body`'s regex, `test_references`'s test-path pattern, and any
`dropped` verdict's textual turn attribution), and `requires_verification` for
`unverified` or an evaluator with nothing to check. This is set by which
evaluator produced the verdict, never by whether it passed — a heuristic that
happens to be right is still tiered `heuristic`. See Track 1 below: tiering
communicates how much to trust a *correctly targeted* check, not whether
extraction targeted the right thing.

## Track 1: privacy boundary

Two assumptions the original design made are false and must not come back:

1. That a transcript could be shipped wholesale to a hosted model for
   extraction. It never was — `extract_requirements` only ever receives
   user-prompt turns, never assistant text or `tool_use` payloads — but even
   that narrower slice must not leave the machine by default.
2. That all verdicts carry equal epistemic weight. They do not: a graph
   relation lookup and a regex-over-source-text heuristic are different kinds
   of evidence, and presenting them identically overstates the heuristic ones.
   See `evidence_class` above.

Consequences, both enforced in code, not just documented:

- `--backend local` (Ollama, `http://localhost`) is the default and sends
  nothing off-machine. `--backend api` additionally requires `--allow-external`
  and prints a warning naming the destination and model before the call. One
  could argue Anthropic isn't a *new* external party for a transcript that
  Claude Code itself authored — but this tool does not rely on that argument by
  default; the user opts in explicitly, every invocation.
- Redacted or missing transcript/graph fields must degrade, not crash or
  silently drop a requirement. A nulled field yields a row with a reduced
  `evidence_class`, or a `False`/`None` evaluator result, never an exception
  and never a vanished row. See `test_drift.py` for the redacted/missing-field
  cases this is tested against.

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
- Extraction backend is swappable: local (Ollama) by default, hosted fallback
  via the Anthropic Messages API (`claude-haiku-4-5`). Both are JSON-schema-
  constrained at decode time (Ollama `format`, Anthropic `output_config`).
  One call, regardless of backend. Sarvam AI was tried and rejected: its
  small-context conversational variant can't hold this project's own
  checkpoint transcripts, and its reasoning variant burns its entire token
  budget on hidden chain-of-thought before producing output, even on a
  one-sentence prompt.
- Parse the real ndjson field names. Do not assume a schema.
- Exit non-zero if any verdict is `absent` or `dropped`.
- `--backend api` requires `--allow-external`; `--backend local` needs nothing
  extra and is the default. See Track 1 above.
- A `dropped`/`absent` on a symbol that plainly exists (e.g. a constructor
  parameter or attribute like `timeout`) is usually an extraction-assertion
  mismatch, not an evaluator bug: this graph only exposes functions/classes/
  methods as symbols for most languages, not parameters or attributes, so
  naming one in `symbol_exists` is structurally unwinnable. Fix the extraction
  prompt's definition of "symbol"; do not patch the evaluator to guess harder,
  and do not use `evidence_class` to paper over a mistargeted assertion.
- Tests: `python -m unittest test_drift`. Stdlib only; `requests.post` and
  `run_entire`/`extract_requirements` are mocked so nothing touches a network
  or shells out to `entire`.

## Working style

Build in stages and stop for review between them. Do not scaffold the full
verdict set before a single transcript has been parsed successfully.

Stage order: transcript + graph parsing → extraction → `landed`/`absent` →
`dropped` → `partial`/`unverified`.

## Note

This repo has `entire enable` active. The session building this tool is itself a
Checkpoint, and a valid input to the tool. If you drop a requirement while
refactoring, `drift` will eventually find it.
