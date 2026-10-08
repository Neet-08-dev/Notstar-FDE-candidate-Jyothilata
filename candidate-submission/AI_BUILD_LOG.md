# AI development-tool build log

## Tools and workflow

**Tool:** Claude (Anthropic), chat interface with its code sandbox. No other coding tool. No model runs inside the submitted agent.

**Delegated to the AI:** almost all of `starter/agent.py`, the eval runner, the eval cases, and the first drafts of these documents. It ran the code in its sandbox before handing it to me.

**Done by me:** set up the repo and ran the API, the agent, the unit tests and both eval suites on my own machine. Worked through the brief, `docs/API.md`, the policies, the data and the public cases until I could explain them. Built the first gates one at a time and ran the evals after each. Traced the P01 booking and the P05 credit call by call against the code and the audit log.

**Approximate time:** about 10 hours in total. Around 4.5 hours went into understanding the problem, the terms and the documents. The remaining 5.5 hours went into building, testing and writing.
## Evidence of judgment

**1. Safety detection**
- Asked: how should the assistant detect safety words reliably?
- Produced: a rule check using the emergency list from the live policy plus extra word roots ("smok", "spark").
- Verified: public case P07, E05 (different wording), E30 (new word added to the live policy).
- Kept. It is oversensitive on purpose, recorded as a known limitation with its own case (E07).

**2. Refusing another customer's records**
- Asked: refuse records outside the requester's account.
- Produced first: read the record by ID and treat the API's refusal as the stop signal.
- Problem: the API audits blocked attempts too, and the brief counts an attempt as a failure.
- Changed: scoped `search_records` first, `get_record` only for the requester's own records. Cases E02 and E20 check `get_record` is never called for a cross-customer request.

**3. Requests the agent does not understand**
- Produced first: hand every unrecognised request to operations.
- Problem: a scaffold unit test failed, and the brief says handing off everything does not meet the task.
- Changed: it now asks one specific question and writes nothing. One test assertion was updated from `error` to `needs_clarification` (listed in `README.md`).

## Most consequential AI-generated mistake

**A real credit was issued in answer to a question.**

- Wrong implementation: a message counted as a credit order if it had a money word and an action word anywhere. "Request" was on the action list.
- Effect: "Can you confirm whether a credit was applied to I001? I think it was $50." with the subject "Request" caused a real $50 credit.
- How it was found: after every case passed, the AI tool ran 16 messages in new wording and checked each write in the audit log. I did not find this one myself.
- Regression guard: case E11 checks zero credits, zero approvals, and no `issue_credit` or `request_approval` call. Before and after runs are in `evals/results/`.

## Productivity and limits

- - **Observed:** from 0/8 to 8/8 public and 35 authored cases took about 10 hours, including the time to understand the problem.
- **Subjective estimate:** I could not have written this much working, tested code alone in this time.
- **Where I did not delegate:** running the checks and understanding the system. I re-ran the final suite on my own machine: 8/8 public and 175/175 authored.
- **Limits:** I know the code from reading, running and tracing it, not from writing it. The cases and the code come from the same author and tool, which is a weakness stated in `EVALS.md`.