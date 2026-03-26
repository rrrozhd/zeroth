---
name: progress-logger
description: >
  MANDATORY skill that enforces progress logging on every implementation
  iteration. Use this skill whenever you are implementing, modifying, fixing,
  or completing ANY task that maps to a phase in the Zeroth project. This skill
  must fire BEFORE you report completion and AFTER every meaningful unit of work.
  Trigger on: finishing a task, completing a checklist item, passing or failing
  tests, creating or modifying deliverables, hitting a blocker, or changing
  approach. If you are doing implementation work in this project, this skill
  applies — no exceptions.
disable-model-invocation: false
user-invocable: true
allowed-tools: Bash, Read, Glob, Grep, Edit, Write
argument-hint: "[phase/task completed or status update]"
---

# Progress Logger

Every implementation iteration must update `PROGRESS.md` (root) and, when
applicable, produce artifacts in the relevant phase's `artifacts/` directory.

## File Layout

```
PROGRESS.md                          ← single source of truth for all progress + log
phases/phase-1-foundation/PLAN.md    ← detailed requirements (read only when working on Phase 1)
phases/phase-1-foundation/artifacts/ ← test output, evidence
phases/phase-2-execution/PLAN.md
phases/phase-2-execution/artifacts/
phases/phase-3-control/PLAN.md
phases/phase-3-control/artifacts/
phases/phase-4-deployment/PLAN.md
phases/phase-4-deployment/artifacts/
phases/phase-5-integration/PLAN.md
phases/phase-5-integration/artifacts/
```

An agent assigned to task "2E" only needs to read:
1. The 2E section of `PROGRESS.md` (to see current state)
2. `phases/phase-2-execution/PLAN.md` (for detailed requirements)

Do NOT read `PLAN.md` (root) — it is the master spec, not an implementation guide.

## The Protocol

### Step 1 — Read Current State

Read the relevant section of `PROGRESS.md` to see what's done, in-progress, or blocked.
If you need detailed requirements, read the phase's `PLAN.md`.

### Step 2 — Update Checkboxes in PROGRESS.md

Edit `PROGRESS.md`:

1. **Completed** — change `[ ]` to `[x]` for tasks you finished
2. **In-progress** — change `[ ]` to `[~]` for tasks you started but haven't finished
3. **Blocked** — change `[ ]` to `[-]` and add a note explaining the blocker
4. **Artifacts** — only mark `**Artifact:**` lines `[x]` when evidence exists

Rules:
- Never mark `[x]` unless the code exists AND compiles/runs
- Never mark an artifact `[x]` unless the file is in `artifacts/` or tests pass
- If you deviated from the plan, add a `> **Note:**` line under the task
- Update after each completed item, not in batches

### Step 3 — Append to the Log

Append an entry to the `## Log` section at the bottom of `PROGRESS.md`:

```markdown
### YYYY-MM-DD HH:MM — [brief title]
**Phase/Tasks:** 1A, 2E, etc.
**Status:** completed | in-progress | blocked
**What:** [concrete changes — files created/modified, functions implemented]
**Tests:** [pass/fail/not run]
**Artifacts:** [files added to phases/phase-N-*/artifacts/]
**Blockers:** [none or description]
**Next:** [what should happen next]
```

### Step 4 — Produce Artifacts (when applicable)

When an `**Artifact:**` line requires evidence:

- **Test results:** `phases/phase-N-*/artifacts/test-{task}-{date}.txt`
- **Validation reports:** `phases/phase-N-*/artifacts/validation-{desc}.json`
- **Build evidence:** `phases/phase-N-*/artifacts/build-{desc}.txt`

Capture output even on failure — failed artifacts are valid evidence.

## Anti-Patterns

| Bad | Good |
|-----|------|
| Mark done without code existing | Only mark done when code is saved and works |
| Skip artifact production | Capture test output when artifact checklist requires it |
| Vague log ("worked on models") | Specific log ("defined Graph model in src/models/graph.py, 11 fields, serialization test passes") |
| Batch updates at end of session | Update after each completed item |
| Mark artifact done without file | Artifact checkbox = file exists in artifacts/ |
| Read root PLAN.md to start working | Read only the phase PLAN.md you need |
| Read all PROGRESS.md sections | Read only the section for your task |

## Example

After implementing the Graph model and passing serialization tests:

1. Read 1A section of `PROGRESS.md`
2. Mark "Define canonical Graph model" as `[x]`
3. Mark "Artifact: serialization round-trip tests pass" as `[x]`
4. Save test output to `phases/phase-1-foundation/artifacts/test-1a-graph-serialization-2026-03-18.txt`
5. Append log entry with what was done, test results, next steps
