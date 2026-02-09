# TASK: Review iteration {ITERATION}

Run metadata:
- run_id: {RUN_ID}
- repo_root: {REPO_ROOT}
- current_branch: {CURRENT_BRANCH}
- base_repo_sha: {REPO_SHA}

## What changed (optional diff summary from orchestrator)
{DIFF_SUMMARY_TEXT}

## Review instructions
Review the Planner’s artifacts for repo impact completeness and plan/testability.
You MUST review:
- {ARTIFACTS_DIR}/impact.md
- {ARTIFACTS_DIR}/plan.md
- {ARTIFACTS_DIR}/acceptance_criteria.md
- {ARTIFACTS_DIR}/open_questions.yaml
- {ARTIFACTS_DIR}/assumptions.md
- {ARTIFACTS_DIR}/repo_digest.md
- {ARTIFACTS_DIR}/repo_digest.json

## Required checks (mark pass/fail in the checklist)
1) impact.md has all required headings and covers major surfaces.
2) impact items are named with "Impact: <name>" entries.
3) plan milestones/tasks reference Impact: <name>.
4) acceptance criteria are measurable/testable.
5) testing implications and rollout/migration are present where needed.
6) open_questions.yaml contains blocking questions for unresolved material ambiguities (and none are missing).
7) assumptions.md contains all non-trivial suppositions (no silent invention).
8) plan aligns with repo_digest (build/test reality, boundaries).

## Output reminders
- Only modify {ARTIFACTS_DIR}/review.md (unless explicitly allowed otherwise).
- End by printing <reviewer_summary_json>…</reviewer_summary_json> as specified in your system prompt.
