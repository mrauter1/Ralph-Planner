# TASK: Planning iteration {ITERATION}

Run metadata:
- run_id: {RUN_ID}
- repo_root: {REPO_ROOT}
- current_branch: {CURRENT_BRANCH}
- base_repo_sha: {REPO_SHA}
- allow_code_changes: {ALLOW_CODE_CHANGES}
- allowed_path_prefixes: {ALLOWED_PATH_PREFIXES_CSV}
- disallowed_path_prefixes: {DISALLOWED_PATH_PREFIXES_CSV}

## Objective
Update the planning artifacts to produce a repo-grounded implementation plan for:
{TASK_TITLE}

## User request / goal statement
{USER_GOAL_TEXT}

## Constraints
{CONSTRAINTS_TEXT}

## What to read (context pointers)
You MUST read:
- {ARTIFACTS_DIR}/repo_digest.md
- {ARTIFACTS_DIR}/repo_digest.json
- {ARTIFACTS_DIR}/decision_log.md
- {ARTIFACTS_DIR}/open_questions.yaml
- {ARTIFACTS_DIR}/assumptions.md
- {ARTIFACTS_DIR}/impact.md
- {ARTIFACTS_DIR}/plan.md
- Any repository files needed to validate repo reality (build/test entry points, interfaces, boundaries).

## Required actions (in order)
1) Update impact.md first:
   - Ensure all required headings exist.
   - Ensure IMP-xx items are present for all major impact surfaces.
2) Update plan.md:
   - Every milestone/task references IMP-xx.
   - Include alternatives considered and why chosen approach fits THIS repo.
3) Update acceptance_criteria.md:
   - Make criteria measurable and testable.
   - Tie criteria to milestones when possible.
4) Update open_questions.yaml:
   - Add blocking questions only when they materially affect scope/cost/architecture/safety.
   - For each question, include best_supposition and impact_if_wrong.
5) Update assumptions.md:
   - No implicit assumptions remain.
6) Update synthesis.md:
   - Summarize key deltas since last iteration in <10 bullets.
7) Update manifest.json:
   - Include run_id, iteration, base_repo_sha, timestamps (if provided), and quick counts:
     - blocking_questions_count
     - files_changed_count

## Planning-only safety rule
If allow_code_changes is false:
- DO NOT modify files outside allowed_path_prefixes.
- If you believe code changes are necessary even in planning-only mode, create a Must-Ask blocking question describing:
  - what code change you propose,
  - why it is necessary,
  - risk/benefit.

## Output reminders
- Primary output is file edits.
- End by printing <planner_summary_json>…</planner_summary_json> as specified in your system prompt.
