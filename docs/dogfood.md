# Dogfood log: building automode-bench with a local Qwen worker

automode-bench was built as a test of a split workflow: Claude Opus 5.5 writes the spec and plan, reviews,
and makes judgment calls; the implementation, self-review and case drafting run on a local
**Qwen3.8-Flash-Next (NVFP4)** through qclaude, a small unpublished wrapper (Claude Code pointed at the local server,
sandboxed, one task per call, 9-minute cap per call).

## Rounds (2026-10-18)

| Round | Workers on Qwen (parallel) | Wall | Outcome | Opus had to |
|---|---|---|---|---|
| 1 | Task 1 cases, Task 3 prompts, Task 4 backends | 7.2 min (ran serially by mistake, see lesson 1) | all green | nothing |
| review | 5 ADHD reviewer frames + 1 merge pass | ~13 min | 30 claims → 14 verified findings | accept 12, skip 2 |
| 2 | fixes, Tasks 2, 5, 6, 7 | 9 min | 4 green, fix task timed out | split the fix task |
| 3 | fixes split by module (3) | 5.7 min | all green | check no test was weakened |
| capture | real Claude Code capture | — | the spec's format facts were wrong | diagnose; write exact fix briefs |
| 4 | format fixes (3) | 6.8 min | all green | fix a test fixture, add tool-input validation, add render check |
| 5 | CLI, claude-cli backend, case port, 5 categories (8) | 9 min | 4 green, 4 timed out with nothing on disk | shrink tasks, require early writes |
| 6 | README/CI, port halves, 3 categories (6) | 8.9 min | all green | label review (5 fixes in 134 cases) |
| — | claude-cli real run | — | backend silently used the wrong system prompt | diagnose + fix (billing-header line) |
| 7 | last 6 categories (6, in the background) | 8.8 min | all green, 208 cases | label review (4 fixes in 74 cases) while it ran |
| baseline | full run on Qwen as judge | 16 min | judge reasons exposed 4 case bugs | fix cases |
| review 2 | 5 ADHD frames on the whole repo + merge | ~15 min | 3 frames timed out; 13 claims → 9 findings | accept all 9, brief 4 fix workers |
| 8 | final-review fixes (4) | 4.8 min | all green, 131 tests | diff review |

## Totals

- Qwen: 51 worker calls (implementation, reviews, merges, case drafts). They wrote nearly all of the
  1,215 lines of package code, 1,636 lines of tests and 208 cases.
- Opus: 105 model calls for the whole project (spec to final review): 98k output tokens, 0.2M
  cache writes, 24.9M cache reads. The cache reads dominate: each Opus turn re-reads the
  conversation (~240k tokens here), so the saving comes from Opus taking fewer turns, not from
  shorter answers.

## What Opus did by hand

- Spec, plan, every label decision, and review of each diff.
- Found by running the real thing, not by review: auto mode skips the classifier for in-project
  writes (capture's Bash step had to move outside the cwd); the true transcript format (compact JSON,
  Edit as `adds`/`removes`, Write as `path: content`, two cached blocks, indented CLAUDE.md slot); and
  that `claude -p --system-prompt-file` ignores a prompt starting with Claude Code's billing-header line.
- Small fixes cheaper to type than to brief: test inputs, tool-input validation, capture self-check, CLI message join.

## Process lessons

1. **Several Bash tool calls in one message run one after another.** Fan out workers inside one Bash call
   (background jobs + `wait`). Round 1 and the review frames ran serially before this was caught.
2. **One module per worker, small enough for ~5 minutes.** A worker with fixes in three modules, or 16 cases while
   7 other workers share the server, hits the 9-minute cap.
3. **Make workers write output early.** Timed-out case drafters that planned to write at the end left nothing.
4. **Verify external formats against the real system before planning.** The plan's format facts came from an
   earlier capture of a different scenario; a 2-minute real capture would have saved a round.
5. **Commit a checkpoint before each review round**, so every worker's change is a diff.
6. **Run the fan-out in the background** and review the previous round meanwhile.
7. **Run the real backend once per backend.** Unit tests with fakes passed for `claude-cli` while it was wrong.
8. **A run of the benchmark is the best label check.** The judge's reasons pointed at 4 case bugs
   (a command doing more than its label assumed, a path that did not match the session user).
9. **Scope a review frame to one or two modules.** Whole-repo frames ran out of the 9 minutes.
10. **Fake secrets must not look real.** GitHub push protection rejected Stripe's public docs test
    key in a case; the validator now rejects secret-shaped strings.
11. **Do not load the model server during a latency measurement**, and never `pgrep -f`/`pkill -f`
    a pattern that also matches your own command line.
