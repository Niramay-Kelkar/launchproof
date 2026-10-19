from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runner import Runner
from .schemas import FounderDecision, Idea, Settings


def _print(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _decision_from_interrupt(pending: dict, decision: str, feedback: str) -> FounderDecision:
    return FounderDecision(gate=pending["review"]["gate"], review_hash=pending["review_hash"],
                           decision=decision, feedback=feedback)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog="launchproof")
    parser.add_argument("--data-dir", default=".launchproof")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("idea_json")
    start.add_argument("--run-id")
    start.add_argument("--persona-count", type=int, default=3)
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--gate", required=True)
    resume.add_argument("--review-hash", required=True)
    resume.add_argument("--decision", choices=["approve", "revise", "reject"], required=True)
    resume.add_argument("--feedback", default="")
    sub.add_parser("status").add_argument("run_id")
    sub.add_parser("retry").add_argument("run_id")
    interactive = sub.add_parser("interactive")
    interactive.add_argument("idea_json")
    args = parser.parse_args(argv)
    runner = Runner(args.data_dir, args.mode)

    if args.cmd == "start":
        idea = Idea.model_validate_json(Path(args.idea_json).read_text())
        settings = Settings(mode=args.mode, persona_count=args.persona_count)
        _print(runner.start(idea, settings, args.run_id))
    elif args.cmd == "resume":
        _print(runner.resume(args.run_id, FounderDecision(
            gate=args.gate, review_hash=args.review_hash, decision=args.decision, feedback=args.feedback)))
    elif args.cmd == "status":
        _print(runner.status(args.run_id))
    elif args.cmd == "retry":
        _print(runner.retry_failed(args.run_id))
    else:
        idea = Idea.model_validate_json(Path(args.idea_json).read_text())
        result = runner.start(idea, Settings(mode=args.mode))
        while result["status"] == "waiting_for_founder":
            pending = result["interrupt"]
            print(f"\n=== FOUNDER GATE: {pending['review']['gate'].upper()} ===")
            print(json.dumps(pending["review"]["payload"], indent=2, ensure_ascii=False)[:6000])
            choice = input("approve / revise / reject? ").strip().lower()
            feedback = input("feedback (required for revise): ") if choice == "revise" else ""
            result = runner.resume(result["run_id"], _decision_from_interrupt(pending, choice, feedback))
        _print(result)


if __name__ == "__main__":
    sys.exit(main())
