"""Run one LAB task through Claude Code (headless `claude -p`).

    python -m claude_code.run_claude_code all --task corporate-ma/<task> --run-id <id> \
        --results-root <dir> --model <model> [--harness-install <dir>]

Mirrors `opencode.run_opencode`: same task loading, same prompt, same results/<run_id>/ layout
(config.json, metrics.json, transcript.jsonl, output/), so `run_eval` scores a Claude Code run
unchanged. What differs is the boundary. OpenCode is fenced to the workspace by
OPENCODE_PERMISSION; Claude Code has no such fence, so the run is split in two phases that a
deployment can put in different containers:

    prepare  needs the LAB checkout: loads task.json, copies the documents (read-only), the
             base skills and the document parsers into the workspace, writes PROMPT.md.
    run      needs ONLY the workspace: runs Claude Code, nudges it to completion, and writes
             the artifacts. task.json -- which carries the grading criteria -- is not in it.

`all` runs both in one process, for a host where the agent is trusted not to look around.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NUDGE = (
    "Continue the assignment to completion now. Do not ask for confirmation. "
    "If every deliverable has already been written to the output directory, "
    "reply with exactly: DONE"
)

ENV_DELTA = """\
# Harness notes (this run)

You are running under Claude Code. Three details differ from the description
above — where they conflict, **these win**:

1. **Paths are real and absolute.** The shell starts in the workspace root, whose
   absolute path is given in the assignment. `documents/` and `output/` are
   subdirectories of it. There is no path rewriting.

2. **Deliverables are not auto-routed.** Every deliverable must be written to
   `output/<filename>` explicitly (or to its absolute path). Anything left
   outside `output/` is not graded.

3. **Reading binary documents.** For `.docx`, `.pdf`, `.pptx`, and `.xlsx`, run:

       python3 scripts/read_doc.py <path> --in host

   which prints the extracted text. The Read tool is still right for plain
   text, markdown, and code.
"""

# Claude Code tools kept out of a LAB run so it stays tool-matched to the native harness
# (bash/read/write/edit/glob/grep + skills); network tools are also blocked by the pod's policy.
DISALLOWED_TOOLS = ("WebFetch", "WebSearch", "NotebookEdit")
_READ_TOOLS = {"Read"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}


def compose_prompt(skill_mode: str = "native") -> str:
    """LAB's base system prompt (+ inline skill manuals) + the Claude Code environment delta.

    Needs the LAB checkout on the path; used by `prepare` and by the adapter's base harness.
    """
    from harness.run import DEFAULT_SKILLS, SYSTEM_PROMPT_PREAMBLE, load_skills

    parts = [SYSTEM_PROMPT_PREAMBLE.strip()]
    if skill_mode == "inline":
        parts.append(load_skills(list(DEFAULT_SKILLS)).strip())
    parts.append(ENV_DELTA.strip())
    return "\n\n".join(p for p in parts if p) + "\n"


def build_prompt(instructions: str, workspace: Path) -> str:
    """The assignment message, worded exactly as the OpenCode runner words it."""
    return (
        f"=== WORKSPACE ===\n{workspace}\n\n"
        f"=== DOCUMENTS (read-only) ===\n{workspace / 'documents'}\n\n"
        f"=== OUTPUT DIRECTORY (deliverables go here) ===\n{workspace / 'output'}\n\n"
        f"=== ASSIGNMENT ===\n{instructions}"
    )


# -- prepare ---------------------------------------------------------------------------------


def prepare(args) -> Path:
    from harness.run import BENCH_ROOT, DEFAULT_SKILLS, SKILLS_DIR, load_task

    task = load_task(task_name=args.task)
    results_dir = Path(args.results_root) / args.run_id
    workspace = Path(args.workspace) if args.workspace else results_dir / "workspace"
    docs = workspace / "documents"
    (workspace / "output").mkdir(parents=True, exist_ok=True)
    if docs.exists():
        shutil.rmtree(docs)
    shutil.copytree(task["docs_dir"], docs)
    for p in docs.rglob("*"):
        if p.is_file():
            p.chmod(0o444)

    # the harness: a materialised ClaudeCodeSubstrate install, or the base body
    if args.harness_install:
        shutil.copytree(args.harness_install, workspace, dirs_exist_ok=True)
    else:
        hdir = workspace / ".claude" / "harness"
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / "systemprompt.md").write_text(compose_prompt(args.skill_mode), encoding="utf-8")
        (workspace / "CLAUDE.md").write_text("@.claude/harness/systemprompt.md\n", encoding="utf-8")
    # base skills are LAB's, not the harness's: always present, never editable. Their manuals
    # name helper scripts as skills/<n>/scripts/..., so those land exactly there (as the
    # OpenCode runner's _copy_skill_scripts does); native mode also installs the manual itself.
    for name in DEFAULT_SKILLS:
        src = SKILLS_DIR / name
        if (src / "scripts").is_dir():
            shutil.copytree(src / "scripts", workspace / "skills" / name / "scripts", dirs_exist_ok=True)
        if args.skill_mode == "native":
            dest = workspace / ".claude" / "skills" / name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / "SKILL.md", dest / "SKILL.md")
    # document reading: the runner's read_doc.py plus LAB's own parser module, nothing else
    (workspace / "scripts").mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).resolve().parent.parent / "opencode" / "scripts" / "read_doc.py",
                 workspace / "scripts" / "read_doc.py")
    shutil.copytree(BENCH_ROOT / "sandbox" / "parsers", workspace / ".lab" / "sandbox" / "parsers",
                    dirs_exist_ok=True)

    (workspace / "PROMPT.md").write_text(build_prompt(task["instructions"], workspace), encoding="utf-8")
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "config.json").write_text(json.dumps({
        "runner": "claude_code",
        "model": args.model,
        "task": args.task,
        "run_id": args.run_id,
        "max_turns": args.max_turns,
        "skill_mode": args.skill_mode,
        "skills": list(DEFAULT_SKILLS),
        "tools_disallowed": list(DISALLOWED_TOOLS),
        "harness_install": str(args.harness_install or ""),
        "doc_parser": "host (in the rollout's own sandbox)",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    return workspace


# -- run -------------------------------------------------------------------------------------


def iter_events(stream: Path):
    if not stream.exists():
        return
    for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse(stream: Path) -> dict:
    """Fold a stream-json session (possibly several resumed invocations) into a summary."""
    steps = in_tok = out_tok = 0
    cost = 0.0
    session_id, texts, calls, pending = "", [], [], {}
    for ev in iter_events(stream):
        session_id = ev.get("session_id") or session_id
        kind = ev.get("type")
        content = (ev.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            steps += 1
            for part in content:
                if part.get("type") == "text" and part.get("text", "").strip():
                    texts.append(part["text"])
                elif part.get("type") == "tool_use":
                    call = {"name": part.get("name", ""), "input": part.get("input") or {},
                            "output": "", "status": "completed"}
                    pending[part.get("id")] = call
                    calls.append(call)
        elif kind == "user" and isinstance(content, list):
            for part in content:
                if part.get("type") == "tool_result" and part.get("tool_use_id") in pending:
                    c = pending.pop(part["tool_use_id"])
                    out = part.get("content")
                    c["output"] = out if isinstance(out, str) else json.dumps(out)[:20000]
                    c["status"] = "error" if part.get("is_error") else "completed"
        elif kind == "result":
            u = ev.get("usage") or {}
            in_tok += int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
                + int(u.get("cache_creation_input_tokens") or 0)
            out_tok += int(u.get("output_tokens") or 0)
            cost += float(ev.get("total_cost_usd") or 0.0)
    return {"session_id": session_id, "steps": steps, "input_tokens": in_tok,
            "output_tokens": out_tok, "cost": cost, "tool_calls": calls, "texts": texts,
            "final_text": texts[-1] if texts else ""}


def to_transcript(stream: Path, transcript: Path) -> None:
    """transcript.jsonl in the native loop's record shapes: assistant / tool / usage."""
    records, calls = [], {}
    for ev in iter_events(stream):
        kind = ev.get("type")
        content = (ev.get("message") or {}).get("content")
        if kind == "assistant" and isinstance(content, list):
            for part in content:
                if part.get("type") == "text" and part.get("text", "").strip():
                    records.append({"role": "assistant", "timestamp": None, "text": part["text"]})
                elif part.get("type") == "tool_use":
                    calls[part.get("id")] = {"role": "tool", "timestamp": None, "tool": part.get("name"),
                                             "input": part.get("input") or {}, "output": "",
                                             "status": "completed"}
        elif kind == "user" and isinstance(content, list):
            for part in content:
                if part.get("type") == "tool_result" and part.get("tool_use_id") in calls:
                    rec = calls.pop(part["tool_use_id"])
                    out = part.get("content")
                    rec["output"] = out if isinstance(out, str) else json.dumps(out)[:20000]
                    rec["status"] = "error" if part.get("is_error") else "completed"
                    records.append(rec)
        elif kind == "result":
            records.append({"role": "usage", "timestamp": None, "tokens": ev.get("usage") or {},
                            "cost": ev.get("total_cost_usd")})
    with open(transcript, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def doc_coverage(summary: dict, documents_dir: Path) -> tuple[int, int]:
    """(documents_read, total_documents), matched on filename stem as the OpenCode runner does."""
    all_docs = [p for p in Path(documents_dir).rglob("*") if p.is_file()]
    stems = {p.stem: p.name for p in all_docs}
    seen: set[str] = set()
    for call in summary["tool_calls"]:
        blob = str(call["input"].get("file_path") or "") if call["name"] in _READ_TOOLS \
            else json.dumps(call["input"])
        seen |= {name for stem, name in stems.items() if stem in blob}
    return len(seen), len(all_docs)


def invoke(argv_tail: list[str], *, model: str, max_turns: int, env: dict, cwd: Path,
           stream: Path, log: Path, timeout: int, append: bool) -> int:
    argv = ["claude", *argv_tail, "--model", model, "--output-format", "stream-json", "--verbose",
            "--max-turns", str(max_turns), "--dangerously-skip-permissions",
            "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    with open(stream, "a" if append else "w", encoding="utf-8") as out, open(log, "a", encoding="utf-8") as err:
        try:
            return subprocess.run(argv, stdout=out, stderr=err, env=env, cwd=cwd,
                                  timeout=max(30, timeout)).returncode
        except subprocess.TimeoutExpired:
            return 124


def run(args, workspace: Path) -> dict:
    results_dir = Path(args.results_root) / args.run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    stream, log = results_dir / "claude_session.jsonl", results_dir / "claude_stderr.log"
    output_dir = workspace / "output"
    env = dict(os.environ)
    env["LAB_BENCH_ROOT"] = str(workspace / ".lab")  # read_doc.py --in host finds the parsers here
    env.setdefault("HOME", str(workspace / ".home"))
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    if args.session_id:
        session = ["--session-id", args.session_id]
    else:
        session = []

    t0 = time.time()
    prompt = (workspace / "PROMPT.md").read_text(encoding="utf-8")
    rc = invoke(["-p", prompt, *session], model=args.model, max_turns=args.max_turns, env=env,
                cwd=workspace, stream=stream, log=log, timeout=args.timeout, append=False)
    summary = parse(stream)
    for _ in range(args.max_continue):
        remaining = args.timeout - (time.time() - t0)
        if remaining < 60 or not summary["session_id"]:
            break
        if any(f.is_file() for f in output_dir.rglob("*")) and summary["final_text"].strip() == "DONE":
            break
        before = len(summary["tool_calls"])
        rc = invoke(["-p", NUDGE, "--resume", summary["session_id"]], model=args.model,
                    max_turns=args.max_turns, env=env, cwd=workspace, stream=stream, log=log,
                    timeout=int(remaining), append=True)
        summary = parse(stream)
        if len(summary["tool_calls"]) == before:
            break
    wall = time.time() - t0

    graded = results_dir / "output"
    graded.mkdir(parents=True, exist_ok=True)
    for src in output_dir.rglob("*"):
        if src.is_file():
            dest = graded / src.relative_to(output_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    to_transcript(stream, results_dir / "transcript.jsonl")
    docs_read, docs_total = doc_coverage(summary, workspace / "documents")
    names = [c["name"] for c in summary["tool_calls"]]
    metrics = {
        "runner": "claude_code",
        "model": args.model,
        "task": args.task,
        "run_id": args.run_id,
        "turn_count": summary["steps"],
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "reasoning_tokens": 0,
        "total_tokens": summary["input_tokens"] + summary["output_tokens"],
        "wall_clock_seconds": round(wall, 1),
        # the CLI's own estimate; behind a gateway the gateway's spend records are authoritative
        "cost_usd": round(summary["cost"], 4),
        "finished_cleanly": rc == 0,
        "exit_code": rc,
        "tool_calls": len(summary["tool_calls"]),
        "documents_read": docs_read,
        "total_documents": docs_total,
        "harness_component_use": {
            "skill_calls": names.count("Skill"),
            "task_calls": names.count("Task"),
            "write_edit_calls": sum(names.count(t) for t in _WRITE_TOOLS),
        },
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    api_errors = [t for t in summary["texts"] if t.lstrip().startswith("API Error")]
    metrics["api_errors"] = len(api_errors)
    if api_errors and summary["final_text"].lstrip().startswith("API Error"):
        # Claude Code reports subtype "success" even when the session ended on a provider
        # refusal; such a run is unfinished, and a missing metrics.json keeps it uncached
        print(f"session ended on a provider error ({api_errors[-1][:160]!r}); not writing "
              f"metrics.json", file=sys.stderr)
        return metrics
    if summary["steps"] == 0:
        # same rule as the OpenCode runner: no parseable events means a dead chain, and a
        # missing metrics.json is what tells the adapter not to cache this run
        print(f"no parseable events in {stream}; not writing metrics.json", file=sys.stderr)
        return metrics
    (results_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Run a LAB task through Claude Code")
    ap.add_argument("phase", choices=("prepare", "run", "all"))
    ap.add_argument("--task", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--workspace", default="", help="default: <results>/<run_id>/workspace")
    ap.add_argument("--harness-install", default="",
                    help="a materialised ClaudeCodeSubstrate install; omit for the base harness")
    ap.add_argument("--skill-mode", choices=("inline", "native"), default="native")
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--max-continue", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--session-id", default="")
    a = ap.parse_args(argv)
    workspace = Path(a.workspace) if a.workspace else Path(a.results_root) / a.run_id / "workspace"
    if a.phase in ("prepare", "all"):
        workspace = prepare(a)
    if a.phase in ("run", "all"):
        m = run(a, workspace)
        print(json.dumps({k: m[k] for k in ("turn_count", "tool_calls", "documents_read",
                                            "total_documents", "exit_code")}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
