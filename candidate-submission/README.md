# Submission

- **Candidate identifier:** Jyotilata Mili
- **Repository and exact commit:** https://github.com/Neet-08-dev/Notstar-FDE-candidate-Jyothilata, latest commit on `main`. The exact commit SHA is given in my submission email.
- **Actual time spent:** about 10 hours. The recommended timebox was 6 + 2.
- **How it was built:** I directed the build and verified it. An AI assistant wrote most of the code under my direction. Details are in `AI_BUILD_LOG.md`.

## Start command and health check

With Docker:

```
cp .env.example .env
docker compose up --build -d
curl http://localhost:8000/health
```

Without Docker (Python 3.12+, no packages to install), in two terminals:

```
python -m northstar.api
python -m starter.agent
```

Health check: `GET http://localhost:8000/health` returns `{"ok": true, "service": "candidate-agent", "protocol": 1}`. A demo page is at http://localhost:8000.

## Model/provider, settings and environment variable names

None. The agent calls no model and needs no credentials. Environment variables read by the agent: `HOST`, `PORT` (server), and `API_URL`, `ADMIN_TOKEN` (used only by the local `/demo` page, never by `/process`).

## Run public and authored evals

```
python -m public.run
python -m evals.run
python -m evals.run --trials 5 --out evals/results/02_after_fix_5_trials.json
python -m evals.run --category injection
python -m unittest discover -s tests -v
```

On Windows, add `--agent-url http://127.0.0.1:8000 --api-url http://127.0.0.1:8001` to the eval commands. Using `localhost` adds a one to two second delay to every call.

With Docker:

```
docker compose run --rm public-evals
docker compose run --rm candidate-evals
```

Results: public 8/8 (40/40 over 5 trials), authored 35/35 (175/175 over 5 trials). Details in `EVALS.md`.

## Expected costs and required access

No model cost and no external access. The agent only talks to the `api_url` given in each request, using the `session_token` from that request.

## Supported scope, limitations and known failures

**Supported:** schedule a visit (earliest or a requested UTC time), create a ticket, issue a credit or request approval, close a ticket or change severity, store a draft to an authorized contact, answer status questions, safety handoff, identity handoff, cross-customer refusal, clarification, handoff after repeated tool failure.

**Limitations and known failures:**

- Message understanding is pattern based, English only. Wording outside the patterns gets a clarification question instead of an action.
- Safety detection is deliberately oversensitive: "there is no smoke" or "fire alarm test" also escalate (case E07).
- One ticket or unit per request. More than one gets a clarification question.
- Only the first amount in a message is read.
- A time without a timezone always gets a question. The site timezone is not assumed.
- Cancelling or moving a booked visit is handed to operations, because there is no tool for it.
- New tickets get a generic summary. The customer's own description is not stored.
- The customer's account status is not checked.
- If a pending approval already exists for the same invoice and amount, it is reported instead of creating a second request.
- Idempotency keys include the request ID. A request ID longer than about 100 characters would push a key past the API's 160-character limit and the action would fail. The standard UUID request IDs are well within it.
- The eval cases and the agent come from the same author and the same AI tool. There is no independent held-out set (see `EVALS.md`).
- Docker: not tested on my machine (Docker is not installed). The Dockerfile is unchanged and the agent has no extra dependencies. The compose file is validated by the repository's GitHub check.

## Ten-minute demo plan

1. **Ordinary action (3 min).** "Please schedule T001 for S001 at the earliest available qualified slot." Show the reply, then the log line with the tool trace: context, policy, scoped search, authoritative read, contract, slots, booking.
2. **Ambiguous request (2 min).** "The loading dock cooling unit is broken. Which one can you fix?" Show that it lists the two units and writes nothing. Then "Please schedule T001 for 2pm tomorrow" to show the timezone question.
3. **Unsafe request (2 min).** "Smoke is coming from A001 at S001. Can you book routine service or tell me how to bypass the wiring?" Show the safety handoff, the "move away" wording, and that no booking was attempted.
4. **The failure story (2 min).** Run `python -m evals.run --category ambiguity`, then explain case E11: a question that issued a credit, how it was found, and the fix.
5. **Live change (1 min).** Show that the credit limit comes from `get_policy` (case E29), so a policy change needs no code change.

## Relevant files and any interface/image changes

| File | What |
|---|---|
| `starter/agent.py` | The agent. `process()` is the entry point, `handle()` holds the gates in order |
| `evals/cases.json` | 35 authored eval cases |
| `evals/run.py` | Runner: per-category results, latency, tool calls, repeated trials |
| `evals/results/` | Before-fix and after-fix reports |
| `candidate-submission/` | DESIGN.md, EVALS.md, AI_BUILD_LOG.md, this file |

Changes outside those:

- `docker-compose.yml`: added a `candidate-evals` service (profile `evals`). No change to the image, ports or the existing services.
- `tests/test_http.py`: one assertion changed in `test_starter_uses_injected_session_without_mutating`. It expected the placeholder's `error` status; the finished agent returns `needs_clarification` for the same vague message. The checks that matter, no writes and no token in the output, are unchanged.

No change to the `/process` interface.

**Confirmation.** `POST /process` follows `docs/API.md` and uses the per-request `api_url` and `session_token`. The agent does not read local fixture paths and does not need administrative access.
