# TASK: Reconcile Reviewer Must-fix items (iteration {ITERATION})

Run metadata:
- run_id: {RUN_ID}
- base_repo_sha: {REPO_SHA}

## Inputs
You MUST read:
- {ARTIFACTS_DIR}/review.md
- All planning artifacts listed in the Planner iteration prompt

## Objective
Address ONLY the Must-fix items from review.md.
- Apply minimal changes needed to clear Must-fix.
- If a Must-fix cannot be resolved without user input, convert it into a blocking question in open_questions.yaml with best_supposition and impact_if_wrong, and note it in synthesis.md.

## Output reminders
- Update impact.md before plan.md if impact changes are required.
- End by printing <planner_summary_json>…</planner_summary_json>.
