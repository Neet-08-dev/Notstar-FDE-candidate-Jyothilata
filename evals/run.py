"""Runner for the candidate-authored eval cases.

For each case it:
  1. asks the mock API for a fresh session set up with the case's fixture
  2. sends the request to the agent
  3. freezes the session and reads what REALLY changed (the snapshot)
  4. grades the snapshot and the reply against the case's expectations

The grading looks at the system's own records and audit log, not at what the agent
claims, and it does not import or reuse any of the agent's logic.

Usage:
    python -m evals.run                      all cases, one trial each
    python -m evals.run --trials 5           every case five times
    python -m evals.run --category safety    only one category
    python -m evals.run --out evals/report.json
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from northstar.http import request_json
from public.check import public_grade


def grade(case, response, snapshot):
    """The public checks, plus two extra checks of our own."""
    result = public_grade(case, response, snapshot)
    checks = result["checks"]

    # Extra check 1: words that must NOT appear in the reply (leaks, false claims)
    reply = str(response.get("reply", "")).lower()
    banned_words = case["expect"].get("reply_must_not_contain", [])
    position = 0
    for word in banned_words:
        checks["no_leak_" + str(position)] = word.lower() not in reply
        position = position + 1

    # Extra check 2: an upper limit on tool calls, to catch retry loops
    max_calls = case["expect"].get("max_tool_calls")
    if max_calls is not None:
        checks["tool_calls"] = len(snapshot["audit"]) <= max_calls

    passed = True
    for name in checks:
        if not checks[name]:
            passed = False
    result["passed"] = passed
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://localhost:8000")
    parser.add_argument("--api-url", default="http://localhost:8001")
    parser.add_argument("--agent-api-url", help="API URL reachable from the agent, if different from the runner URL")
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "local-development-only"))
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--category", help="Run only the cases in this category")
    parser.add_argument("--cases", default=str(Path(__file__).parent / "cases.json"))
    parser.add_argument("--out", help="Optional JSON report file")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    rows = []

    for case in cases:
        if args.category and case.get("category") != args.category:
            continue
        for trial in range(args.trials):
            request_id = str(uuid.uuid4())
            fixture = dict(case.get("fixture", {}))
            fixture["request_id"] = request_id
            session = request_json(args.api_url + "/admin/sessions", fixture, args.admin_token)

            request = dict(case["request"])
            request["id"] = request_id
            agent_payload = {
                "api_url": args.agent_api_url or args.api_url,
                "session_token": session["session_token"],
                "run_id": request_id,
                "request": request,
            }

            started = time.perf_counter()
            response = {}
            error = None
            try:
                response = request_json(args.agent_url.rstrip("/") + "/process", agent_payload, timeout=60)
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)

            try:
                snapshot = request_json(
                    args.api_url + "/admin/sessions/" + session["session_id"] + "/finalize", {}, args.admin_token
                )
                if not isinstance(response, dict):
                    response = {}
                result = grade(case, response, snapshot)
                if error:
                    result["passed"] = False
                row = {
                    "case_id": case["id"],
                    "category": case.get("category", "uncategorised"),
                    "title": case.get("title", ""),
                    "trial": trial + 1,
                    "passed": result["passed"],
                    "checks": result["checks"],
                    "status": response.get("status"),
                    "tool_calls": len(snapshot["audit"]),
                    "latency_ms": latency_ms,
                    "error": error,
                }
                rows.append(row)
                failed_checks = []
                for name in result["checks"]:
                    if not result["checks"][name]:
                        failed_checks.append(name)
                if row["passed"]:
                    print(case["id"], "PASS", row["status"], str(row["tool_calls"]) + " calls", flush=True)
                else:
                    print(case["id"], "FAIL", row["status"], "failed checks:", failed_checks, flush=True)
            finally:
                request_json(
                    args.api_url + "/admin/sessions/" + session["session_id"], token=args.admin_token, method="DELETE"
                )

    # ---- Summary per category ----
    categories = {}
    for row in rows:
        name = row["category"]
        if name not in categories:
            categories[name] = {"passed": 0, "total": 0, "latency": [], "calls": []}
        categories[name]["total"] = categories[name]["total"] + 1
        if row["passed"]:
            categories[name]["passed"] = categories[name]["passed"] + 1
        categories[name]["latency"].append(row["latency_ms"])
        categories[name]["calls"].append(row["tool_calls"])

    print("")
    print("Category".ljust(18), "Passed".ljust(10), "Avg ms".ljust(10), "Max ms".ljust(10), "Avg tool calls")
    summary = {}
    for name in sorted(categories):
        entry = categories[name]
        average_ms = round(sum(entry["latency"]) / len(entry["latency"]), 1)
        max_ms = round(max(entry["latency"]), 1)
        average_calls = round(sum(entry["calls"]) / len(entry["calls"]), 1)
        summary[name] = {
            "passed": entry["passed"], "total": entry["total"],
            "avg_latency_ms": average_ms, "max_latency_ms": max_ms, "avg_tool_calls": average_calls,
        }
        print(name.ljust(18), (str(entry["passed"]) + "/" + str(entry["total"])).ljust(10),
              str(average_ms).ljust(10), str(max_ms).ljust(10), average_calls)

    passed_count = 0
    for row in rows:
        if row["passed"]:
            passed_count = passed_count + 1
    print("")
    print("Candidate scenarios:", passed_count, "/", len(rows), "(" + str(args.trials) + " trial(s) per case)")

    if args.out:
        report = {"passed": passed_count, "total": len(rows), "trials_per_case": args.trials,
                  "by_category": summary, "results": rows}
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if passed_count == len(rows):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
