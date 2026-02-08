#!/usr/bin/env python3
import argparse
import datetime
import atexit
import configparser
import importlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_DIGEST_JSON = "repo_digest.json"
REPO_DIGEST_MD = "repo_digest.md"
PLANNING_DIR = "planning"
ARTIFACTS_SUBDIR = "planning/artifacts"
STATE_FILE = "planning/state.json"

ARTIFACT_FILES = [
    "impact.md",
    "plan.md",
    "acceptance_criteria.md",
    "assumptions.md",
    "open_questions.yaml",
    "decision_log.md",
    "repo_digest.md",
    "repo_digest.json",
    "synthesis.md",
    "review.md",
    "manifest.json",
]

REQUIRED_IMPACT_HEADINGS = [
    "# Impact Analysis",
    "## Likely Code/Module Touch Points",
    "## Interfaces and Contracts Affected",
    "## Dependency and Compatibility Consequences",
    "## Testing Implications",
    "## Rollout / Migration Plan",
    "## Operational Risks",
]

REQUIRED_PLAN_HEADINGS = [
    "# Implementation Plan",
    "## Goals",
    "## Non-Goals",
    "## Approach (with alternatives considered)",
    "## Milestones and Tasks",
    "## Validation and Testing Plan",
    "## Rollout Plan (if applicable)",
    "## Open Questions (pointer to open_questions.yaml)",
]

DEFAULT_ALLOWED_PREFIXES = ["planning/"]
DEFAULT_DISALLOWED_PREFIXES = ["src/", "infra/"]

LOCK_FILE = "planning/.lock"


def load_yaml_module():
    if importlib.util.find_spec("yaml") is None:
        return None
    return importlib.import_module("yaml")


def load_toml_module():
    if importlib.util.find_spec("tomllib") is not None:
        return importlib.import_module("tomllib")
    if importlib.util.find_spec("tomli") is not None:
        return importlib.import_module("tomli")
    return None


def load_psutil_module():
    if importlib.util.find_spec("psutil") is None:
        return None
    return importlib.import_module("psutil")


class RunnerError(Exception):
    pass


class AgentRunner:
    def run(
        self,
        *,
        role: str,
        prompt: str,
        repo_root: Path,
        env: Dict[str, str],
    ) -> Tuple[int, str, str]:
        raise NotImplementedError


class DryRunner(AgentRunner):
    def run(
        self,
        *,
        role: str,
        prompt: str,
        repo_root: Path,
        env: Dict[str, str],
    ) -> Tuple[int, str, str]:
        artifacts_dir = Path(env["ARTIFACTS_DIR"])
        if role == "planner":
            write_dry_planner_artifacts(artifacts_dir, env)
            return 0, "", ""
        if role == "reviewer":
            write_dry_review(artifacts_dir, env)
            return 0, "", ""
        raise RunnerError(f"Unknown role: {role}")


class CodexRunner(AgentRunner):
    def run(
        self,
        *,
        role: str,
        prompt: str,
        repo_root: Path,
        env: Dict[str, str],
    ) -> Tuple[int, str, str]:
        cmd = ["codex", "exec", prompt]
        return run_subprocess(cmd, repo_root, env)


class ClaudeRunner(AgentRunner):
    def run(
        self,
        *,
        role: str,
        prompt: str,
        repo_root: Path,
        env: Dict[str, str],
    ) -> Tuple[int, str, str]:
        system_prompt_file = (
            "planning/prompts/planner_system.md"
            if role == "planner"
            else "planning/prompts/reviewer_system.md"
        )
        cmd = [
            "claude",
            "-p",
            prompt,
            "--append-system-prompt-file",
            system_prompt_file,
        ]
        return run_subprocess(cmd, repo_root, env)


def run_subprocess(
    cmd: List[str], repo_root: Path, env: Dict[str, str]
) -> Tuple[int, str, str]:
    merged_env = os.environ.copy()
    merged_env.update(env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RunnerError(f"Runner command not found: {cmd[0]}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repo-first planning loop")
    parser.add_argument("--repo", required=True, help="Path to repo root")
    parser.add_argument(
        "--backend",
        required=True,
        choices=["codex", "claude", "dry"],
        help="Agent backend",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--branch", help="Target branch name")
    group.add_argument("--use-current-branch", action="store_true")
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--max-review-rounds", type=int, required=True)
    parser.add_argument("--allow-code-changes", action="store_true")
    parser.add_argument(
        "--require-clean-tree",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--noninteractive", action="store_true")
    parser.add_argument("--task-title", default="Planning task")
    parser.add_argument(
        "--user-goal-text",
        default="Generate a repository-grounded implementation plan.",
    )
    parser.add_argument(
        "--constraints-text",
        default="Planning-only; do not modify non-planning files without explicit approval.",
    )
    return parser.parse_args()


def ensure_git_repo(repo_root: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError("Not inside a git repository")


def acquire_lock(repo_root: Path) -> Path:
    lock_path = repo_root / LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for _ in range(2):
        try:
            fd = os.open(lock_path, flags)
        except FileExistsError:
            contents = lock_path.read_text().strip()
            pid, start_time = parse_lock_contents(contents)
            if pid and pid_matches(pid, start_time):
                raise RuntimeError(f"Planning lock already held by PID {pid}")
            lock_path.unlink(missing_ok=True)
            continue
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(format_lock_contents())
            return lock_path
    raise RuntimeError("Unable to acquire planning lock")


def release_lock(lock_path: Path) -> None:
    if lock_path.exists():
        lock_path.unlink()


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    psutil_module = load_psutil_module()
    if psutil_module is not None:
        return psutil_module.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def pid_matches(pid: int, start_time: Optional[float]) -> bool:
    if not pid_exists(pid):
        return False
    if start_time is None:
        return True
    psutil_module = load_psutil_module()
    if psutil_module is None:
        sys.stderr.write(
            "Warning: 'psutil' not found. Cannot reliably check for stale locks "
            "against PID reuse. Install 'psutil' for robust lock validation.\n"
        )
        return True
    try:
        proc = psutil_module.Process(pid)
        return abs(proc.create_time() - start_time) < 1.0
    except psutil_module.Error:
        return False


def format_lock_contents() -> str:
    pid = os.getpid()
    psutil_module = load_psutil_module()
    if psutil_module is not None:
        try:
            proc = psutil_module.Process(pid)
            return f"{pid}\n{proc.create_time()}\n"
        except psutil_module.Error:
            return f"{pid}\n"
    return f"{pid}\n"


def parse_lock_contents(contents: str) -> Tuple[Optional[int], Optional[float]]:
    if not contents:
        return None, None
    lines = contents.splitlines()
    try:
        pid = int(lines[0])
    except (ValueError, IndexError):
        return None, None
    start_time = None
    if len(lines) > 1:
        try:
            start_time = float(lines[1])
        except ValueError:
            start_time = None
    return pid, start_time


def current_branch(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def ensure_branch(repo_root: Path, requested: Optional[str]) -> str:
    branch = current_branch(repo_root)
    if requested and requested != branch:
        raise RuntimeError(
            f"Current branch '{branch}' does not match requested '{requested}'"
        )
    if branch in {"main", "master"}:
        raise RuntimeError("Refusing to run on main/master branch")
    return branch


def git_head_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_status_porcelain(repo_root: Path) -> List[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def enforce_clean_tree(repo_root: Path, allowed_prefixes: List[str]) -> None:
    dirty = []
    for line in git_status_porcelain(repo_root):
        path = line[3:]
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            dirty.append(path)
    if dirty:
        raise RuntimeError(
            "Working tree has changes outside planning/: " + ", ".join(dirty)
        )


def ensure_dirs(repo_root: Path) -> Path:
    artifacts_dir = repo_root / ARTIFACTS_SUBDIR
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir = repo_root / PLANNING_DIR / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    state_path = repo_root / STATE_FILE
    if not state_path.exists():
        state_path.write_text(json.dumps({"iteration": 0}, indent=2))
    return artifacts_dir


def load_state(repo_root: Path) -> Dict[str, int]:
    state_path = repo_root / STATE_FILE
    if not state_path.exists():
        return {"iteration": 0}
    return json.loads(state_path.read_text())


def save_state(repo_root: Path, state: Dict[str, int]) -> None:
    state_path = repo_root / STATE_FILE
    state_path.write_text(json.dumps(state, indent=2))


def limited_tree_summary(repo_root: Path, max_depth: int = 2, max_entries: int = 200) -> List[str]:
    entries: List[str] = []
    base_parts = len(repo_root.parts)
    for root, dirs, files in os.walk(repo_root):
        if ".git" in dirs:
            dirs.remove(".git")
        rel_parts = Path(root).parts[base_parts:]
        depth = len(rel_parts)
        if depth > max_depth:
            dirs[:] = []
            continue
        for name in sorted(dirs + files):
            rel_path = str(Path(root).joinpath(name).relative_to(repo_root))
            entries.append(rel_path)
            if len(entries) >= max_entries:
                return entries
    return entries


def find_build_entries(repo_root: Path) -> Dict[str, object]:
    candidates = [
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "Pipfile",
        "package.json",
        "Makefile",
        "poetry.lock",
        "tox.ini",
        "noxfile.py",
    ]
    found: List[str] = []
    commands: List[str] = []
    for name in candidates:
        path = repo_root / name
        if path.exists():
            found.append(name)
            commands.extend(extract_build_commands(path))
    return {"files": found, "commands": sorted(set(commands))}


def extract_build_commands(path: Path) -> List[str]:
    name = path.name
    if name == "package.json":
        return extract_package_json_commands(path)
    if name == "pyproject.toml":
        return extract_pyproject_commands(path)
    if name == "tox.ini":
        return extract_tox_commands(path)
    if name == "Makefile":
        return extract_makefile_targets(path)
    return []


def extract_package_json_commands(path: Path) -> List[str]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    scripts = data.get("scripts", {})
    commands = []
    for key in ["test", "build", "lint"]:
        if key in scripts:
            commands.append(f"npm run {key}")
    return commands


def extract_pyproject_commands(path: Path) -> List[str]:
    toml_module = load_toml_module()
    if toml_module is None:
        return []
    data = toml_module.loads(path.read_text())
    commands = []
    if "tool" in data and "poetry" in data.get("tool", {}):
        commands.extend(["poetry install", "poetry run pytest"])
    if "project" in data:
        commands.append("python -m pytest")
    return commands


def extract_tox_commands(path: Path) -> List[str]:
    parser = configparser.ConfigParser()
    parser.read(path)
    if parser.sections():
        return ["tox"]
    return []


def extract_makefile_targets(path: Path) -> List[str]:
    targets = []
    for line in path.read_text().splitlines():
        if re.match(r"^\s*[a-zA-Z0-9_-]+:", line):
            target = line.split(":")[0]
            if target in {"test", "build", "lint"}:
                targets.append(f"make {target}")
    return targets


def identify_boundaries(repo_root: Path) -> Dict[str, List[str]]:
    top_dirs = [p.name for p in repo_root.iterdir() if p.is_dir()]
    surfaces = {
        "config": [d for d in top_dirs if "config" in d or d in {"conf", "settings"}],
        "schemas": [d for d in top_dirs if "schema" in d],
        "migrations": [d for d in top_dirs if "migration" in d],
        "api": [d for d in top_dirs if "api" in d],
    }
    hotspots = [
        d
        for d in top_dirs
        if any(token in d for token in ["auth", "security", "perf", "payment"])
    ]
    return {"surfaces": surfaces, "hotspots": hotspots}


def write_repo_digest(repo_root: Path, artifacts_dir: Path) -> Dict[str, object]:
    sha = git_head_sha(repo_root)
    dirty = git_status_porcelain(repo_root)
    tree = limited_tree_summary(repo_root)
    build_entries = find_build_entries(repo_root)
    boundaries = identify_boundaries(repo_root)

    digest = {
        "repo": {
            "head_sha": sha,
            "dirty": bool(dirty),
            "porcelain": dirty,
        },
        "topology": {
            "tree_summary": tree,
            "key_directories": [p.name for p in repo_root.iterdir() if p.is_dir()],
        },
        "build_test_entrypoints": build_entries,
        "change_surface": boundaries["surfaces"],
        "hotspots": boundaries["hotspots"],
    }

    (artifacts_dir / REPO_DIGEST_JSON).write_text(json.dumps(digest, indent=2))

    digest_md = [
        "# Repo Digest",
        "",
        f"- HEAD SHA: {sha}",
        f"- Dirty: {bool(dirty)}",
        "",
        "## Topology",
        "- Key directories:",
    ]
    for name in digest["topology"]["key_directories"]:
        digest_md.append(f"  - {name}")
    digest_md.append("")
    digest_md.append("- Tree summary (depth-capped):")
    for entry in tree:
        digest_md.append(f"  - {entry}")
    digest_md.append("")
    digest_md.append("## Build/Test Entry Points")
    if build_entries["files"]:
        for entry in build_entries["files"]:
            digest_md.append(f"- {entry}")
    else:
        digest_md.append("- None detected")
    if build_entries["commands"]:
        digest_md.append("")
        digest_md.append("## Inferred Build/Test Commands")
        for cmd in build_entries["commands"]:
            digest_md.append(f"- {cmd}")
    digest_md.append("")
    digest_md.append("## Change Surface Cues")
    for surface, dirs in boundaries["surfaces"].items():
        digest_md.append(f"- {surface}: {', '.join(dirs) if dirs else 'none'}")
    digest_md.append("")
    digest_md.append("## Hotspots")
    digest_md.append(", ".join(boundaries["hotspots"]) if boundaries["hotspots"] else "none")
    digest_md.append("")

    (artifacts_dir / REPO_DIGEST_MD).write_text("\n".join(digest_md))
    return digest


def render_prompt(template_path: Path, values: Dict[str, str]) -> str:
    content = template_path.read_text()
    pattern = re.compile(r"\{([A-Z0-9_]+)\}")

    def replace(match: re.Match) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return pattern.sub(replace, content)


def parse_open_questions(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"questions": [], "_parse_error": "missing"}
    yaml_module = load_yaml_module()
    if yaml_module is None:
        return {"questions": [], "_parse_error": "pyyaml not available"}
    try:
        data = yaml_module.safe_load(path.read_text())
        if not isinstance(data, dict):
            return {"questions": [], "_parse_error": "not a mapping"}
        return data
    except yaml_module.YAMLError:
        return {"questions": [], "_parse_error": "invalid yaml"}


def save_open_questions(path: Path, data: Dict[str, object]) -> None:
    yaml_module = load_yaml_module()
    if yaml_module is None:
        raise RuntimeError("pyyaml not available for writing open_questions.yaml")
    path.write_text(
        yaml_module.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
        )
    )


def blocking_questions_count(path: Path) -> int:
    data = parse_open_questions(path)
    questions = data.get("questions", [])
    return sum(1 for q in questions if q.get("blocking"))


def acceptance_criteria_measurable(path: Path) -> bool:
    if not path.exists():
        return False
    content = path.read_text().lower()
    lines = [line.strip() for line in content.splitlines()]
    criteria_lines = [
        line for line in lines if line.startswith("-") or re.match(r"\d+\.", line)
    ]
    if not criteria_lines:
        return False
    for line in criteria_lines:
        if any(
            token in line
            for token in ["must", "shall", "should", "<=", ">=", "pass", "fail"]
        ):
            return True
        if re.search(r"\d", line):
            return True
    return False


def plan_references_impacts(path: Path) -> bool:
    if not path.exists():
        return False
    return bool(re.search(r"IMP-\d+", path.read_text()))


def milestones_reference_impacts(path: Path) -> List[str]:
    if not path.exists():
        return []
    content = path.read_text().splitlines()
    in_milestones = False
    missing = []
    for line in content:
        if line.strip().startswith("## "):
            in_milestones = line.strip() == "## Milestones and Tasks"
            continue
        if not in_milestones:
            continue
        if line.strip().startswith("-"):
            if not re.search(r"IMP-\d+", line):
                missing.append(line.strip())
    return missing


def impact_has_headings(path: Path) -> List[str]:
    if not path.exists():
        return REQUIRED_IMPACT_HEADINGS
    content = path.read_text()
    return [h for h in REQUIRED_IMPACT_HEADINGS if h not in content]


def validate_artifacts(artifacts_dir: Path) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    for name in ARTIFACT_FILES:
        if not (artifacts_dir / name).exists():
            errors.append(f"Missing required artifact {name}")

    missing_headings = impact_has_headings(artifacts_dir / "impact.md")
    if missing_headings:
        errors.append("impact.md missing headings: " + ", ".join(missing_headings))

    for heading in REQUIRED_PLAN_HEADINGS:
        if (artifacts_dir / "plan.md").exists():
            if heading not in (artifacts_dir / "plan.md").read_text():
                errors.append(f"plan.md missing heading: {heading}")

    if not plan_references_impacts(artifacts_dir / "plan.md"):
        errors.append("plan.md missing IMP-xx references")
    missing_milestones = milestones_reference_impacts(artifacts_dir / "plan.md")
    if missing_milestones:
        errors.append(
            "plan.md milestones missing IMP-xx references: "
            + "; ".join(missing_milestones)
        )

    open_questions = parse_open_questions(artifacts_dir / "open_questions.yaml")
    if "_parse_error" in open_questions:
        errors.append(
            f"open_questions.yaml parse error: {open_questions['_parse_error']}"
        )
    if "questions" not in open_questions:
        errors.append("open_questions.yaml missing 'questions' key")
    else:
        if not isinstance(open_questions.get("questions"), list):
            errors.append("open_questions.yaml 'questions' must be a list")
        else:
            required_fields = {
                "id",
                "blocking",
                "question",
                "best_supposition",
                "impact_if_wrong",
            }
            for index, item in enumerate(open_questions.get("questions", [])):
                missing = required_fields - set(item.keys())
                if missing:
                    question_id = item.get("id", "unknown")
                    errors.append(
                        f"open_questions.yaml question {question_id} at index {index} missing fields: "
                        + ", ".join(sorted(missing))
                    )

    if not acceptance_criteria_measurable(artifacts_dir / "acceptance_criteria.md"):
        errors.append("acceptance_criteria.md lacks measurable criteria")

    if not (artifacts_dir / "impact.md").exists():
        warnings.append("No impact.md for testing implications check")
    else:
        content = (artifacts_dir / "impact.md").read_text()
        if "## Testing Implications" in content and "-" not in content:
            warnings.append("impact.md testing implications appears empty")

    if not (artifacts_dir / "repo_digest.md").exists():
        warnings.append("repo_digest.md missing")

    return errors, warnings


def write_dry_planner_artifacts(artifacts_dir: Path, env: Dict[str, str]) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "impact.md").write_text(
        "\n".join(
            [
                "# Impact Analysis",
                "",
                "## Likely Code/Module Touch Points",
                "- IMP-01: planning/plan_loop.py (orchestrator logic; high confidence)",
                "- IMP-02: planning/prompts/* (prompt templates; high confidence)",
                "",
                "## Interfaces and Contracts Affected",
                "- IMP-03: CLI flags and artifact schemas (medium confidence)",
                "",
                "## Dependency and Compatibility Consequences",
                "- IMP-04: Requires git CLI availability (high confidence)",
                "",
                "## Testing Implications",
                "- IMP-05: Validate artifacts and ensure commit-only planning files",
                "",
                "## Rollout / Migration Plan",
                "- IMP-06: Run on feature branch; no migrations expected",
                "",
                "## Operational Risks",
                "- IMP-07: Runner command missing; handle with failure commit",
            ]
        )
    )
    (artifacts_dir / "plan.md").write_text(
        "\n".join(
            [
                "# Implementation Plan",
                "",
                "## Goals",
                "- Provide a repo-first planning loop with planner/reviewer artifacts.",
                "",
                "## Non-Goals",
                "- No automatic feature implementation.",
                "",
                "## Approach (with alternatives considered)",
                "- Implement a Python orchestrator with pluggable runners; avoid SDK lock-in.",
                "",
                "## Milestones and Tasks",
                "- M1 (IMP-01, IMP-03): Implement CLI, git preflight, and repo digest generation.",
                "- M2 (IMP-02, IMP-05): Generate planner artifacts and run validation.",
                "- M3 (IMP-07, IMP-04): Add reviewer run and failure-safe commits.",
                "",
                "## Validation and Testing Plan",
                "- Run script with --backend dry to verify artifacts and validation output.",
                "",
                "## Rollout Plan (if applicable)",
                "- None for planning-only tooling.",
                "",
                "## Open Questions (pointer to open_questions.yaml)",
                "- See open_questions.yaml.",
            ]
        )
    )
    (artifacts_dir / "acceptance_criteria.md").write_text(
        "\n".join(
            [
                "# Acceptance Criteria",
                "- The script must generate all planning/artifacts files in a single run.",
                "- Each iteration must commit a success or failure message with base SHA.",
                "- Validation failures must be recorded in manifest.json.",
            ]
        )
    )
    (artifacts_dir / "assumptions.md").write_text(
        "\n".join(
            [
                "# Assumptions",
                "- Git CLI is available in the execution environment.",
                "- The repo has a non-main branch checked out for planning.",
            ]
        )
    )
    (artifacts_dir / "open_questions.yaml").write_text("questions: []\n")
    (artifacts_dir / "decision_log.md").write_text("# Decision Log\n")
    (artifacts_dir / "synthesis.md").write_text(
        "# Synthesis\n- Initial dry-run artifacts generated."
    )
    (artifacts_dir / "manifest.json").write_text(
        json.dumps({"status": "placeholder"}, indent=2)
    )


def write_dry_review(artifacts_dir: Path, env: Dict[str, str]) -> None:
    (artifacts_dir / "review.md").write_text(
        "\n".join(
            [
                "# Review",
                "## Summary (1-3 bullets)",
                "- Dry-run review completed; no must-fix items found.",
                "",
                "## Must-fix",
                "- None.",
                "",
                "## Should-fix",
                "- None.",
                "",
                "## Nice-to-have",
                "- None.",
                "",
                "## Checklist (pass/fail per check)",
                "- [pass] impact.md has required headings",
                "- [pass] plan milestones reference IMP-xx",
                "- [pass] acceptance criteria measurable",
                "- [pass] testing implications included",
                "- [pass] open questions captured",
                "- [pass] assumptions documented",
                "- [pass] plan aligns with repo digest",
                "",
                "## Questions for the user (only if truly needed)",
                "- None.",
            ]
        )
    )


def detect_must_fix(review_path: Path) -> int:
    if not review_path.exists():
        return 0
    content = review_path.read_text()
    must_fix_section = re.search(r"## Must-fix(.*?)(##|$)", content, re.S)
    if not must_fix_section:
        return 0
    items = [
        line
        for line in must_fix_section.group(1).splitlines()
        if line.strip().startswith("-") and "None" not in line
    ]
    return len(items)


def parse_must_fix_items(review_path: Path) -> List[str]:
    if not review_path.exists():
        return []
    content = review_path.read_text()
    must_fix_section = re.search(r"## Must-fix(.*?)(##|$)", content, re.S)
    if not must_fix_section:
        return []
    items = []
    for line in must_fix_section.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("-") and "None" not in stripped:
            items.append(stripped.lstrip("-").strip())
    return items


def update_manifest(
    artifacts_dir: Path,
    *,
    iteration: int,
    base_sha: str,
    validation_errors: List[str],
    validation_warnings: List[str],
    blocking_questions: int,
    must_fix_count: int,
    run_id: str,
    backend: str,
    lock_path: Optional[Path],
    planner_summary: Optional[Dict[str, object]],
    reviewer_summary: Optional[Dict[str, object]],
) -> None:
    manifest = {
        "iteration": iteration,
        "base_repo_sha": base_sha,
        "validation_status": "pass" if not validation_errors else "fail",
        "validation_errors": validation_errors,
        "validation_warnings": validation_warnings,
        "blocking_questions_count": blocking_questions,
        "reviewer_must_fix_count": must_fix_count,
        "run_id": run_id,
        "backend": backend,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "lock_path": str(lock_path) if lock_path else None,
        "planner_summary": planner_summary,
        "reviewer_summary": reviewer_summary,
    }
    (artifacts_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def stage_planning_files(repo_root: Path) -> None:
    subprocess.run(
        ["git", "add", ARTIFACTS_SUBDIR, STATE_FILE],
        cwd=repo_root,
        check=True,
    )


def enforce_commit_scope(
    repo_root: Path, allowed_prefixes: List[str], allow_code_changes: bool
) -> List[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    staged = [line for line in result.stdout.splitlines() if line.strip()]
    if allow_code_changes:
        return []
    disallowed = [
        path for path in staged if not any(path.startswith(p) for p in allowed_prefixes)
    ]
    if disallowed:
        subprocess.run(["git", "reset", "--"] + disallowed, cwd=repo_root, check=True)
    return disallowed


def commit_iteration(
    repo_root: Path,
    *,
    iteration: int,
    status: str,
    base_sha: str,
    summary: str,
) -> None:
    message = f"plan(iter {iteration:03d}): {status} | {summary} | base={base_sha}"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=repo_root,
        check=True,
    )


def collect_diff_summary(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def parse_summary_block(stdout: str, tag: str) -> Tuple[Optional[Dict[str, object]], str]:
    pattern = re.compile(
        rf"<{tag}>(.*?)</{tag}>",
        re.S,
    )
    match = pattern.search(stdout or "")
    if not match:
        return None, f"Missing <{tag}> block"
    try:
        return json.loads(match.group(1).strip()), ""
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON in <{tag}> block: {exc}"


def run_planner(
    *,
    runner: AgentRunner,
    repo_root: Path,
    prompt: str,
    env: Dict[str, str],
    validation_errors: List[str],
    validation_warnings: List[str],
) -> Optional[Dict[str, object]]:
    try:
        planner_code, planner_out, planner_err = runner.run(
            role="planner", prompt=prompt, repo_root=repo_root, env=env
        )
        if planner_code != 0:
            validation_errors.append(f"Planner failed: {planner_err.strip()}")
        parsed, error = parse_summary_block(planner_out, "planner_summary_json")
        if error:
            validation_warnings.append(error)
        else:
            return parsed
    except RunnerError as exc:
        validation_errors.append(str(exc))
    return None


def run_reviewer_rounds(
    *,
    runner: AgentRunner,
    repo_root: Path,
    prompt: str,
    env: Dict[str, str],
    prompt_values: Dict[str, str],
    max_rounds: int,
    validation_errors: List[str],
    validation_warnings: List[str],
) -> Tuple[int, Optional[Dict[str, object]]]:
    reviewer_summary: Optional[Dict[str, object]] = None
    must_fix_count = 0
    for round_index in range(max_rounds):
        before_reviewer = set(changed_files(repo_root))
        try:
            reviewer_code, reviewer_out, reviewer_err = runner.run(
                role="reviewer",
                prompt=prompt,
                repo_root=repo_root,
                env=env,
            )
            if reviewer_code != 0:
                validation_errors.append(f"Reviewer failed: {reviewer_err.strip()}")
            parsed, error = parse_summary_block(reviewer_out, "reviewer_summary_json")
            if error:
                validation_warnings.append(error)
            else:
                reviewer_summary = parsed
        except RunnerError as exc:
            validation_errors.append(str(exc))
        after_reviewer = set(changed_files(repo_root))
        reviewer_changes = sorted(after_reviewer - before_reviewer)
        allowed_reviewer_changes = {
            "planning/artifacts/review.md",
            "planning/artifacts/synthesis.md",
        }
        if reviewer_changes and not set(reviewer_changes).issubset(
            allowed_reviewer_changes
        ):
            validation_errors.append(
                "Reviewer modified disallowed files: " + ", ".join(reviewer_changes)
            )
        must_fix_count = detect_must_fix(Path(env["ARTIFACTS_DIR"]) / "review.md")
        if must_fix_count == 0:
            break
        if round_index + 1 >= max_rounds:
            break
        reconcile_prompt = render_prompt(
            repo_root / PLANNING_DIR / "prompts" / "reconcile_planner.md",
            prompt_values,
        )
        try:
            runner.run(
                role="planner",
                prompt=reconcile_prompt,
                repo_root=repo_root,
                env=env,
            )
        except RunnerError as exc:
            validation_errors.append(str(exc))
    return must_fix_count, reviewer_summary


def run_planning_iteration(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    artifacts_dir: Path,
    branch: str,
    iteration: int,
    allowed_prefixes: List[str],
    runner: AgentRunner,
    lock_path: Path,
) -> Tuple[bool, List[str], List[str]]:
    base_sha, run_id, prompt_values = prepare_iteration_context(
        args=args,
        repo_root=repo_root,
        artifacts_dir=artifacts_dir,
        branch=branch,
        iteration=iteration,
    )
    planner_prompt = render_prompt(
        repo_root / PLANNING_DIR / "prompts" / "iteration_planner.md",
        prompt_values,
    )
    env = {
        "ARTIFACTS_DIR": str(artifacts_dir),
    }
    validation_errors: List[str] = []
    validation_warnings: List[str] = []
    planner_summary: Optional[Dict[str, object]] = None
    reviewer_summary: Optional[Dict[str, object]] = None

    planner_summary = run_planner(
        runner=runner,
        repo_root=repo_root,
        prompt=planner_prompt,
        env=env,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
    )
    if not args.allow_code_changes:
        disallowed_changes = find_disallowed_changes(repo_root, allowed_prefixes)
        if disallowed_changes:
            validation_errors.append(
                "Planner modified disallowed files: " + ", ".join(disallowed_changes)
            )

    reviewer_prompt = render_prompt(
        repo_root / PLANNING_DIR / "prompts" / "iteration_reviewer.md",
        prompt_values,
    )
    must_fix_count, reviewer_summary = run_reviewer_rounds(
        runner=runner,
        repo_root=repo_root,
        prompt=reviewer_prompt,
        env=env,
        prompt_values=prompt_values,
        max_rounds=args.max_review_rounds,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
    )

    if must_fix_count > 0:
        try:
            converted = add_blocking_questions_from_review(
                artifacts_dir,
                parse_must_fix_items(artifacts_dir / "review.md"),
            )
            if converted:
                validation_warnings.append(
                    "Reviewer must-fix converted to blocking questions"
                )
        except RuntimeError as exc:
            validation_errors.append(str(exc))

    blocking_answered = handle_blocking_questions(
        artifacts_dir, noninteractive=args.noninteractive
    )
    if blocking_answered:
        planner_summary = (
            run_planner(
                runner=runner,
                repo_root=repo_root,
                prompt=planner_prompt,
                env=env,
                validation_errors=validation_errors,
                validation_warnings=validation_warnings,
            )
            or planner_summary
        )

    artifact_errors, artifact_warnings = validate_artifacts(artifacts_dir)
    validation_errors.extend(artifact_errors)
    validation_warnings.extend(artifact_warnings)
    blocking_count = blocking_questions_count(artifacts_dir / "open_questions.yaml")

    stage_planning_files(repo_root)
    disallowed_staged = enforce_commit_scope(
        repo_root, allowed_prefixes, args.allow_code_changes
    )
    if disallowed_staged:
        validation_errors.append(
            "Disallowed files were staged and unstaged before commit: "
            + ", ".join(disallowed_staged)
        )

    status = finalize_iteration(
        repo_root=repo_root,
        artifacts_dir=artifacts_dir,
        iteration=iteration,
        base_sha=base_sha,
        run_id=run_id,
        must_fix_count=must_fix_count,
        blocking_count=blocking_count,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        backend=args.backend,
        lock_path=lock_path,
        planner_summary=planner_summary,
        reviewer_summary=reviewer_summary,
    )

    return status == "success", validation_errors, validation_warnings


def prepare_iteration_context(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    artifacts_dir: Path,
    branch: str,
    iteration: int,
) -> Tuple[str, str, Dict[str, str]]:
    base_sha = git_head_sha(repo_root)
    run_id = (
        f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}-"
        f"{random.randint(1000,9999)}"
    )
    write_repo_digest(repo_root, artifacts_dir)
    prompt_values = {
        "RUN_ID": run_id,
        "ITERATION": str(iteration),
        "REPO_ROOT": str(repo_root),
        "CURRENT_BRANCH": branch,
        "REPO_SHA": base_sha,
        "TASK_TITLE": args.task_title,
        "USER_GOAL_TEXT": args.user_goal_text,
        "CONSTRAINTS_TEXT": args.constraints_text,
        "ARTIFACTS_DIR": ARTIFACTS_SUBDIR,
        "ALLOW_CODE_CHANGES": str(args.allow_code_changes).lower(),
        "ALLOWED_PATH_PREFIXES_CSV": ",".join(DEFAULT_ALLOWED_PREFIXES),
        "DISALLOWED_PATH_PREFIXES_CSV": ",".join(DEFAULT_DISALLOWED_PREFIXES),
        "DIFF_SUMMARY_TEXT": collect_diff_summary(repo_root),
    }
    return base_sha, run_id, prompt_values


def finalize_iteration(
    *,
    repo_root: Path,
    artifacts_dir: Path,
    iteration: int,
    base_sha: str,
    run_id: str,
    must_fix_count: int,
    blocking_count: int,
    validation_errors: List[str],
    validation_warnings: List[str],
    backend: str,
    lock_path: Path,
    planner_summary: Optional[Dict[str, object]],
    reviewer_summary: Optional[Dict[str, object]],
) -> str:
    update_manifest(
        artifacts_dir,
        iteration=iteration,
        base_sha=base_sha,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        blocking_questions=blocking_count,
        must_fix_count=must_fix_count,
        run_id=run_id,
        backend=backend,
        lock_path=lock_path,
        planner_summary=planner_summary,
        reviewer_summary=reviewer_summary,
    )
    stage_planning_files(repo_root)

    status = "success" if not validation_errors and must_fix_count == 0 else "failure"
    summary = "update artifacts" if status == "success" else "validation blocked"
    commit_iteration(
        repo_root,
        iteration=iteration,
        status=status,
        base_sha=base_sha,
        summary=summary,
    )
    return status


def changed_files(repo_root: Path) -> List[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    files = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        files.append(line[3:])
    return files


def find_disallowed_changes(
    repo_root: Path, allowed_prefixes: List[str]
) -> List[str]:
    disallowed = []
    for path in changed_files(repo_root):
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            disallowed.append(path)
    return disallowed


def add_blocking_questions_from_review(
    artifacts_dir: Path, must_fix_items: List[str]
) -> bool:
    if not must_fix_items:
        return False
    open_questions_path = artifacts_dir / "open_questions.yaml"
    data = parse_open_questions(open_questions_path)
    if "_parse_error" in data:
        raise RuntimeError(
            f"Cannot append review questions: {data.get('_parse_error')}"
        )
    questions = data.get("questions", [])
    existing_ids = {
        q.get("id") for q in questions if isinstance(q, dict) and "id" in q
    }
    next_index = 1
    while f"Q-{next_index:02d}" in existing_ids:
        next_index += 1
    new_questions = []
    for item in must_fix_items:
        q_id = f"Q-{next_index:02d}"
        next_index += 1
        new_questions.append(
            {
                "id": q_id,
                "blocking": True,
                "question": f"Resolve reviewer must-fix: {item}",
                "best_supposition": "Use the reviewer guidance as-is.",
                "impact_if_wrong": "Plan may be incomplete or invalid until resolved.",
                "needed_by": "before validation",
            }
        )
    questions.extend(new_questions)
    data["questions"] = questions
    save_open_questions(open_questions_path, data)
    synthesis_path = artifacts_dir / "synthesis.md"
    if synthesis_path.exists():
        with synthesis_path.open("a") as handle:
            for item in must_fix_items:
                handle.write(f"\n- Converted reviewer must-fix to blocking question: {item}")
    return True


def handle_blocking_questions(
    artifacts_dir: Path,
    *,
    noninteractive: bool,
) -> bool:
    questions = parse_open_questions(artifacts_dir / "open_questions.yaml").get(
        "questions", []
    )
    blocking = [q for q in questions if q.get("blocking")]
    if not blocking:
        return False

    decision_log = artifacts_dir / "decision_log.md"
    if not decision_log.exists():
        decision_log.write_text("# Decision Log\n")

    if noninteractive:
        with decision_log.open("a") as handle:
            for item in blocking:
                handle.write(
                    f"\n- [{datetime.datetime.utcnow().isoformat()}Z] "
                    f"Q: {item.get('question')}\n"
                    f"  Decision: defaulting to {item.get('best_supposition')}\n"
                    f"  Impact if wrong: {item.get('impact_if_wrong')}\n"
                )
        return True

    with decision_log.open("a") as handle:
        for item in blocking:
            answer = input(
                f"{item.get('question')}\n"
                f"Best supposition: {item.get('best_supposition')}\n"
                f"Impact if wrong: {item.get('impact_if_wrong')}\n"
                "Answer (leave blank to accept best supposition): "
            ).strip()
            decision = answer or item.get("best_supposition")
            handle.write(
                f"\n- [{datetime.datetime.utcnow().isoformat()}Z] "
                f"Q: {item.get('question')}\n"
                f"  Decision: {decision}\n"
                f"  Impact if wrong: {item.get('impact_if_wrong')}\n"
            )
    return True


def build_runner(backend: str) -> AgentRunner:
    if backend == "dry":
        return DryRunner()
    if backend == "codex":
        return CodexRunner()
    if backend == "claude":
        return ClaudeRunner()
    raise RunnerError(f"Unsupported backend: {backend}")


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo).resolve()
    ensure_git_repo(repo_root)

    branch = ensure_branch(repo_root, args.branch if not args.use_current_branch else None)
    allowed_prefixes = DEFAULT_ALLOWED_PREFIXES

    if args.require_clean_tree:
        enforce_clean_tree(repo_root, allowed_prefixes)

    artifacts_dir = ensure_dirs(repo_root)
    state = load_state(repo_root)

    runner = build_runner(args.backend)
    lock_path = acquire_lock(repo_root)
    atexit.register(release_lock, lock_path)

    try:
        for _ in range(args.max_iterations):
            iteration = state.get("iteration", 0) + 1
            state["iteration"] = iteration
            save_state(repo_root, state)

            succeeded, validation_errors, validation_warnings = run_planning_iteration(
                args=args,
                repo_root=repo_root,
                artifacts_dir=artifacts_dir,
                branch=branch,
                iteration=iteration,
                allowed_prefixes=allowed_prefixes,
                runner=runner,
                lock_path=lock_path,
            )
            if validation_warnings:
                print("--- Warnings ---", file=sys.stderr)
                for warning in validation_warnings:
                    print(f"- {warning}", file=sys.stderr)
            if succeeded:
                print("Planning iteration succeeded.", file=sys.stderr)
                return 0
            if validation_errors:
                print("--- Errors ---", file=sys.stderr)
                for error in validation_errors:
                    print(f"- {error}", file=sys.stderr)
            print("Planning iteration failed.", file=sys.stderr)

        return 1
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    sys.exit(main())
