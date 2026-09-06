# drift

## One-sentence summary

drift verifies that requirements stated in an AI coding session's transcript
actually landed in the code that shipped, by extracting them once with an LLM
and checking each one deterministically against a code graph.

## Problem, intended user and why it matters

A diff shows what the code is, never what is missing. In a multi-turn AI
coding session, a requirement stated in turn one can be silently dropped
during a refactor in turn nine, and git records nothing about it.

Intended user: someone auditing an AI agent's coding session — a reviewer, or
the agent's own operator — who needs to know whether stated requirements
actually landed, not just what changed.

Why it matters: the obvious way to check "did the agent do what was asked" is
to have a model read the diff and confirm it. That reproduces the exact
failure mode — confident agreement — that let requirements go missing in the
first place. drift's core bet is separating the one reading-comprehension
task (what was asked) from verification (did it land), and never letting a
model do both.

## Selected Entire track and why Entire is essential

Track 1: Privacy Boundary.

Entire is essential, not incidental: `entire checkpoint explain <id>
--transcript` is the only source of the deterministic, replayable multi-turn
transcript (including `tool_use` payloads) that lets drift see what was asked
and what was written, turn by turn. `entire graph snapshot` / `impact` /
`search` / `diff` are the only source of a deterministic, language-aware code
graph to check those requirements against. The project's own rule — shell out
to `entire` via subprocess, never reimplement it — isn't a style preference;
reimplementing either piece would turn drift into exactly the kind of
unverifiable tool it exists to catch.

## Architecture and main workflow

Single file `drift.py` (~398 lines), Python 3 stdlib + `requests`.

1. `run_entire()` shells out to `entire checkpoint explain <id> --transcript`
   and `entire graph snapshot --repo . --format ndjson`.
2. `parse_transcript()` groups raw JSONL content-blocks into turns (a
   streamed assistant message spanning multiple content-block lines collapses
   into one turn, keyed by API message id).
3. `extract_requirements()` — the one LLM call in the whole program — turns
   user-prompt turns into a requirement + assertion list, schema-constrained
   at decode time (Ollama `format` / Anthropic `output_config`). Backend is
   swappable: `local` (Ollama) or `api` (Anthropic `claude-haiku-4-5`), same
   return shape either way.
4. `build_graph_index()` turns the ndjson snapshot into symbol-by-name and
   edge-by-source lookups.
5. `ASSERTION_HANDLERS` dispatches each of the five closed assertion types
   (`symbol_exists`, `has_inbound_edge`, `calls`, `test_references`,
   `literal_in_body`) to a deterministic evaluator, producing a verdict
   (`landed`/`absent`/`unverified`), an evidence string, and an
   `evidence_class`.
6. `find_dropped_turn()` re-scans `absent` rows backwards through
   `Write`/`Edit` tool_use payloads to catch requirements that existed
   mid-session and vanished before the final graph (`dropped`).
7. `main()` prints the table and exits non-zero on any `absent`/`dropped`
   row.

## Entire Graph findings and verification

- `entire graph capabilities --json` established that Python here is a fully
  semantic language (`CALLS`, `ASYNC_CALLS`, `DATA_FLOWS`, etc.) but has no
  `WRITES_FIELD`/`READS_FIELD`/`ACCESSES` relation support, and the actual
  snapshot never emits function parameters or instance attributes as their
  own symbol records. That single fact is the root cause of today's
  timeout/test bug (see Curveball, below) — confirmed by direct inspection of
  the demo-repo's own `entire graph snapshot` output, not by re-running the
  LLM.
- `entire graph impact` on every function in the extraction backend,
  transcript parser, and verdict/evidence path (saved in
  `.artifacts/graph_impact.txt`) surfaced a real tooling limitation worth
  recording: dict-based dispatch is invisible to static impact analysis.
  `entire graph impact --symbol eval_symbol_exists` (and every other
  `eval_*` handler) reports zero callers, even though they're live code
  reached through the `ASSERTION_HANDLERS` dict — the dict literal itself
  isn't a symbol the graph resolves call edges through.
- `entire graph diff --base main --head stage-1-4-progress` (saved in
  `.artifacts/graph_diff.json`) confirms the change at symbol granularity:
  `parse_transcript`, `build_graph_index`, and `main` bodies changed; one new
  CLAUDE.md section; `test_drift.py` is new. It does not show the
  `ASSERTION_HANDLERS`/`EXTRACTION_SYSTEM` edits as discrete entries, for the
  same module-level-literal limitation noted above — an honest gap in what
  the graph diff can see, not something drift hides.

## Noon Curveball: what changed and how we adapted

**Assumption invalidated:** the design assumed a transcript could be sent
wholesale to a hosted model, and that all verdicts carry equal epistemic
weight. Both are false under the Privacy Boundary constraint.

**Design changed:** local (Ollama) is now the enforced default with no
external call; `--backend api` requires explicit opt-in via
`--allow-external` and prints the destination. Every verdict carries an
`evidence_class` — `confirmed_structural`, `heuristic`, or
`requires_verification` — assigned by which evaluator produced it, not by
whether it passed. Redacted or missing fields degrade to a partial report
instead of crashing or silently dropping rows.

**Why the result is safe:** `confirmed_structural` rows are graph relation
lookups a judge can independently verify; `heuristic` rows are visibly marked
as textual/regex-based and were never claimed as stronger;
`requires_verification` rows were previously silent and now surface instead
of vanishing. Nothing that was `landed`, `absent`, or `dropped` under the old
evidence path changed meaning — only its trust label became explicit.

The commit message's claim that graph impact analysis preceded every edit
(rather than ran alongside it) was independently checked against the
recorded session checkpoint transcript, tool-call by tool-call, before this
document was written — not assumed. All `entire graph` commands (capabilities,
search, impact, and the diagnostic snapshot) ran and were reported at
tool-call indices 9–23; the first `Edit` to `drift.py` was index 34. The claim
held.

## Checkpoint links and what each checkpoint proves

All four checkpoints below were created today (2026-09-06), each tied to one
committed stage on this branch (verified via `entire checkpoint explain
<commit-sha>`, not assumed from commit messages):

| Checkpoint | Commit | Created | What it proves |
|---|---|---|---|
| `01M1TF95A2HA835AYCY52A9RZN` | `ee233a4` | 04:23:03 | Stage 1 landed: transcript JSONL parsing (`parse_transcript`) and graph-snapshot parsing (`build_graph_index`) exist, built before any extraction or evaluation logic. |
| `01M1TNGEWC61CPW6SRDVFBJXMM` | `98d7410` | 06:11:52 | Stage 2 landed: `extract_requirements` with a swappable local/api backend and schema-constrained JSON output, as the single LLM call in the program. |
| `01M1TQ54CDJMHNHTPCR34RVHDY` | `7091380` | 06:40:38 | Stages 3–4 landed, plus a self-caught regression: the transcript shows the extraction prompt over-forcing background prose into requirements and avoiding `unverifiable`, both fixed and re-verified against the tool's own self-referential checkpoint; `find_dropped_turn` added; the timeout/test false-`dropped` bug was identified but explicitly left unfixed when this checkpoint closed. |
| `01M1TRF08X18M5CXSVY310AW0Z` | `8eb8e77` | 07:03:30 | Today's curveball: diagnosed the timeout/test bug as an extraction-assertion mismatch (not an evaluator bug) via direct graph inspection before any edit; then enforced the Privacy Boundary (local-default backend, `--allow-external` gate, `evidence_class` tiering, redaction hardening), backed by 14 passing tests. |

## Setup, run and test instructions

```bash
# requires entire 0.6.2+ and Python 3. On systems with a PEP 668-managed
# system Python (Debian/Ubuntu, current Homebrew Python - a bare `pip
# install` fails there with "externally-managed-environment"), use a venv:
python3 -m venv venv && source venv/bin/activate
pip install requests

# local backend (Ollama on localhost) is the default and sends nothing off-machine
python drift.py --repo <path-to-repo> --checkpoint <checkpoint-id>

# hosted fallback requires explicit opt-in and prints what leaves the machine
python drift.py --repo <path-to-repo> --checkpoint <checkpoint-id> \
  --backend api --allow-external

# tests: stdlib only, no network, no subprocess call to `entire`
python -m unittest test_drift -v
```

## Databricks use, data sources and limitations

Not applicable. This build does not use Databricks. Its only two data sources
are `entire checkpoint explain --transcript` (session transcripts) and
`entire graph snapshot` (code graph), both local and deterministic.

## Known limitations and next steps

- The timeout/test false-`dropped` bug (Stage 3–4) was an
  extraction-assertion mismatch, not an evaluator bug: this graph never
  exposes Python constructor parameters or attributes as checkable symbols,
  so naming one in `symbol_exists` was structurally unwinnable. Fixed by
  narrowing the extraction prompt's definition of "symbol" — but this is a
  probabilistic mitigation on a single non-deterministic LLM call, not a
  guarantee. A future run could still mistarget an assertion, and evidence
  tiering must never be used to excuse that if it happens again.
- Local-model extraction quality is not verified end-to-end in this
  environment. The one locally available model actually exercised against
  the real demo-repo checkpoint during this session, `gemma3:4b`, produced
  malformed, repetition-looped JSON rather than a clean requirement list,
  even with a JSON-schema-constrained decode. (`nemotron-3-nano:4b` was also
  available locally but was not exercised.) All of today's positive
  verification — the corrected evidence table, the timeout/test diagnosis,
  the passing end-to-end tests — used either previously recorded hosted-API
  output or mocked extraction, not a completed real local-model run.
  `--backend local`'s argument handling, URL, and degradation behavior are
  verified; the quality of what a real local model actually extracts is not.
- `entire graph impact` cannot see calls made through the
  `ASSERTION_HANDLERS` dict-dispatch table — a load-bearing part of the
  verdict/evidence path shows zero callers under static impact analysis.
  Anyone adding a sixth assertion type should know graph tooling won't catch
  broken wiring there.
- `partial` is a documented verdict (CLAUDE.md: "symbol exists,
  `has_inbound_edge` fails") that was never implemented as a distinct row in
  `main()` — it is not currently produced. Left untouched as out of scope for
  this checkpoint, but it is a real, pre-existing gap between documented and
  actual behavior.
- **Found while auditing this submission, from a session predating today's
  work:** the `last-prompt` preview field of two earlier checkpoints
  (`01M1TNGEWC61CPW6SRDVFBJXMM`, `01M1TQ54CDJMHNHTPCR34RVHDY`) contains an
  18-character fragment of what appears to be a real Anthropic API key
  (`sk-ant-api03-tHnD…`, truncated by the harness's own preview at that
  length — only ~4 characters beyond the public key-format prefix are
  exposed). It surfaces because `entire checkpoint explain <id> --transcript`
  — the same command drift.py itself runs — returns it. Not introduced by
  today's checkpoint (confirmed absent from both of today's checkpoints);
  not remediated here, since rewriting already-pushed, already-merged
  checkpoint/git history is a destructive, shared-history operation outside
  this checkpoint's scope and requires the repo owner's explicit decision.
  Recommendation: rotate that key.
