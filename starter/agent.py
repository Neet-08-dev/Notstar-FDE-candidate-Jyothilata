import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from northstar.http import RemoteError, ToolClient, request_json


# Settings


MAX_ATTEMPTS = 3

EXTRA_SAFETY_ROOTS = ["smok", "spark", "shock", "burning", "fire", "flame", "electrocut"]


DISPATCH_ROLES = ["customer", "dispatcher", "supervisor"]
BILLING_ROLES = ["customer", "dispatcher", "finance", "supervisor"]

NOTHING_CHANGED = "We have not changed any of your records."

# Small helpers

def call_tool(ctx, tool, arguments):
    """Call one tool. Retry temporary failures up to MAX_ATTEMPTS times.

    Returns two values: (result, error). Exactly one of them is None.
    A retry always sends the same arguments and the same idempotency key.
    """
    attempt = 0
    last_error = None
    while attempt < MAX_ATTEMPTS:
        attempt = attempt + 1
        try:
            result = ctx["client"].call(tool, **arguments)
            ctx["trace"].append({"tool": tool, "attempt": attempt, "ok": True})
            return result, None
        except RemoteError as exc:
            last_error = exc.error
            ctx["trace"].append({"tool": tool, "attempt": attempt, "ok": False, "code": last_error.get("code")})
            if not last_error.get("retryable"):
                break
    return None, last_error


def make_key(ctx, label):
    """A stable label for one action in one request, so a retry cannot do it twice."""
    return ctx["payload"]["request"]["id"] + ":" + label


def build_response(ctx, status, summary, reply):
    return {
        "status": status,
        "summary": summary,
        "reply": reply,
        "evidence": ctx["evidence"],
        "usage": {
            "model": "none",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0,
            "pricing_source": "No model calls; deterministic rules only",
        },
    }


def add_evidence(ctx, collection, record_id):
    item = {"collection": collection, "record_id": record_id}
    if item not in ctx["evidence"]:
        ctx["evidence"].append(item)


def contains_any(text, phrases):
    for phrase in phrases:
        if phrase in text:
            return True
    return False


def format_money(cents):
    dollars = cents // 100
    remainder = cents % 100
    return "$" + str(dollars) + "." + str(remainder).zfill(2)


def format_time(starts_at):
    """Turn 2030-04-08T10:00:00Z into text with an explicit UTC offset."""
    moment = datetime.fromisoformat(starts_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M") + " UTC (" + moment.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00)"


def to_slot_format(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Standard outcomes: refuse, ask, hand off


def refuse(ctx, summary, reply):
    return build_response(ctx, "blocked", summary, reply)


def clarify(ctx, summary, reply):
    return build_response(ctx, "needs_clarification", summary, reply)


def handoff(ctx, queue, reason, reply_start, label, ticket_id=""):
    """Create a human handoff and tell the requester who acts next."""
    arguments = {"queue": queue, "reason": reason, "idempotency_key": make_key(ctx, label)}
    if ticket_id != "":
        arguments["ticket_id"] = ticket_id
    escalation, error = call_tool(ctx, "escalate", arguments)
    if error is not None:
        return build_response(
            ctx,
            "error",
            "Handoff to " + queue + " failed (" + str(error.get("code")) + "). Reason for handoff: " + reason,
            reply_start + " We were unable to reach our " + queue + " team automatically, "
            "so please contact Northstar directly and quote this request.",
        )
    add_evidence(ctx, "escalations", escalation["id"])
    return build_response(
        ctx,
        "escalated",
        "Handed off to " + queue + " as " + escalation["id"] + ". " + reason,
        reply_start + " This has been passed to our " + queue + " team for human review "
        "(reference " + escalation["id"] + "), who will act next.",
    )


def safety_handoff(ctx, reason):
    queue = ctx["policy"]["rules"]["safety"]["queue"]
    return handoff(
        ctx,
        queue,
        reason,
        "Please move away from the equipment now and contact your site emergency personnel. "
        "We have not booked routine service and cannot give repair or bypass instructions.",
        "safety-escalation",
    )

# Reading the message (untrusted text)


def find_safety_signal(subject, body, policy):
    text = (subject + " " + body).lower()

    policy_signals = policy["rules"]["safety"]["emergency_signals"]
    for signal in policy_signals:
        if signal.lower() in text:
            return signal

    for root in EXTRA_SAFETY_ROOTS:
        if root in text:
            return root

    has_smell_word = contains_any(text, ["smell", "odor", "odour"])
    has_hazard_word = contains_any(text, ["gas", "burn"])
    if has_smell_word and has_hazard_word:
        return "gas or burning smell"

    return None


def parse_requested_time(original):
    """Find a requested visit time in the message.

    Returns (kind, value):
      ("none", None)            no time mentioned
      ("exact", "2030-04-08T14:00:00Z")   full date and time with a timezone
      ("time_only", "14:00")    a clock time stated in UTC, no date
      ("date_only", "2030-04-09")
      ("ambiguous", text)       a time with no timezone, so we must ask
    """
    full = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*(Z|UTC|[+-]\d{2}:?\d{2})?", original)
    if full is not None:
        zone = full.group(5)
        if zone is None:
            return "ambiguous", full.group(0).strip()
        if zone in ["Z", "UTC"]:
            offset_text = "+00:00"
        else:
            offset_text = zone
            if ":" not in offset_text:
                offset_text = offset_text[:3] + ":" + offset_text[3:]
        seconds = full.group(4)
        if seconds is None:
            seconds = "00"
        iso_text = full.group(1) + "T" + full.group(2) + ":" + full.group(3) + ":" + seconds + offset_text
        try:
            moment = datetime.fromisoformat(iso_text)
        except ValueError:
            return "ambiguous", full.group(0).strip()
        return "exact", to_slot_format(moment)

    clock_utc = re.search(r"\b(\d{1,2}):(\d{2})\s*(?:UTC|Z)\b", original)
    if clock_utc is not None:
        return "time_only", clock_utc.group(1).zfill(2) + ":" + clock_utc.group(2)

    am_pm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b(\s*UTC)?", original, re.IGNORECASE)
    if am_pm is not None:
        if am_pm.group(4) is None:
            return "ambiguous", am_pm.group(0).strip()
        hour = int(am_pm.group(1)) % 12
        if am_pm.group(3).lower() == "pm":
            hour = hour + 12
        minutes = am_pm.group(2)
        if minutes is None:
            minutes = "00"
        return "time_only", str(hour).zfill(2) + ":" + minutes

    bare_clock = re.search(r"\b\d{1,2}:\d{2}\b", original)
    if bare_clock is not None:
        return "ambiguous", bare_clock.group(0)

    date_only = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", original)
    if date_only is not None:
        return "date_only", date_only.group(1)

    return "none", None


def parse_request(subject, body):
    """Pull out what the requester wants and which records they mention.

    Nothing here is trusted. It only tells us what was ASKED, never what is ALLOWED.
    """
    original = subject + " " + body
    lower_text = original.lower()
    upper_text = original.upper()

    # Record IDs such as T001, S101, A001, I002
    ids = {"tickets": [], "sites": [], "assets": [], "invoices": [], "contacts": []}
    prefix_to_collection = {"T": "tickets", "S": "sites", "A": "assets", "I": "invoices"}
    matches = re.findall(r"\b([TSAI])(\d{3,4})\b", upper_text)
    for match in matches:
        collection = prefix_to_collection[match[0]]
        record_id = match[0] + match[1]
        if record_id not in ids[collection]:
            ids[collection].append(record_id)

    # Tickets created by the system look like NEW-0001
    for record_id in re.findall(r"\bNEW-\d+\b", upper_text):
        if record_id not in ids["tickets"]:
            ids["tickets"].append(record_id)

    for record_id in re.findall(r"\bCONTACT\d+\b", upper_text):
        if record_id not in ids["contacts"]:
            ids["contacts"].append(record_id)

    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", original)

    # Severity such as S1, S2, S3
    severity = None
    severity_match = re.search(r"\bS([123])\b", upper_text)
    if severity_match is not None:
        severity = "S" + severity_match.group(1)

    # Money such as $50, $250.00, $1,000
    amount_cents = None
    amount_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", original)
    if amount_match is not None:
        number_text = amount_match.group(1).replace(",", "")
        if number_text != "":
            amount_cents = int(round(float(number_text) * 100))
    if amount_cents is None:
        words_match = re.search(r"\b([\d,]+(?:\.\d{1,2})?)\s*(?:dollars|usd)\b", lower_text)
        if words_match is None:
            words_match = re.search(r"\busd\s*([\d,]+(?:\.\d{1,2})?)", lower_text)
        if words_match is not None:
            number_text = words_match.group(1).replace(",", "")
            if number_text != "":
                amount_cents = int(round(float(number_text) * 100))

    # --- What kind of request is it ---

    # Credit: an order needs an action verb close to the money word.
    # A question about a past credit ("was a credit applied?") is NOT an order to issue one.
    credit_order = (
        re.search(r"\b(apply|issue|give|grant|add|process)\b.{0,40}\b(credit|refund)", lower_text) is not None
        or re.search(r"\b(want|need|would like|i'd like|claim)\b.{0,30}\b(credit|refund|reimbursement|compensation)", lower_text) is not None
        or re.search(r"\brequest(ing)? (a |an |the )?(service |sla )?(credit|refund)", lower_text) is not None
        or re.search(r"\b(credit|refund|reimburse|compensate) (it|us|me|my|the|our|this|that|invoice)\b", lower_text) is not None
    )
    credit_question = contains_any(lower_text, [
        "whether", "was applied", "been applied", "was issued", "been issued", "was credited", "been credited",
        "did you", "have you", "has a credit", "has the credit", "how much credit",
    ])
    strong_credit_order = re.search(r"\bplease (apply|issue|give|grant|add|process|credit|refund)\b", lower_text) is not None
    wants_credit = credit_order and (not credit_question or strong_credit_order)

    # Visit: only the command forms. "scheduled" or "booked" in a question do not count.
    says_no_visit = contains_any(lower_text, [
        "do not schedule", "don't schedule", "dont schedule", "not schedule",
        "do not book", "don't book", "dont book", "no visit",
        "without scheduling", "ticket only",
    ])
    visit_action = re.search(
        r"\b(schedule|reschedule|book|dispatch|appointment|send (a|an|someone|somebody)|"
        r"need a (visit|technician|engineer)|arrange a (visit|technician|engineer))\b",
        lower_text,
    )
    wants_schedule = visit_action is not None and not says_no_visit

    wants_ticket = (
        re.search(r"\b(create|open|raise|log|file)\b.{0,40}\bticket\b", lower_text) is not None
        or "new ticket" in lower_text
    )

    # Whole words only: "fix" counts, "fixed" does not
    reports_problem = re.search(
        r"\b(broken|stopped|failed|failure|leak|leaking|fault|faulty|outage|breakdown|malfunction|malfunctioning|"
        r"not working|not cooling|not heating|isn't working|is down|fix|repair)\b",
        lower_text,
    ) is not None

    wants_cancel = re.search(r"\bcancel\b", lower_text) is not None
    wants_reschedule = re.search(
        r"\b(reschedule|rebook|move the (visit|appointment|booking)|change the (time|visit|appointment|booking))\b",
        lower_text,
    ) is not None

    wants_resolve = (
        re.search(r"\b(close|resolve)\b", lower_text) is not None
        or re.search(r"\bmark\b.{0,40}\b(resolved|closed|complete|completed|done)\b", lower_text) is not None
    )

    wants_severity_change = re.search(
        r"\b(change|update|set|raise|lower|upgrade|downgrade|reclassify)\b.{0,60}\bs[123]\b", lower_text
    ) is not None

    wants_draft = "draft" in lower_text or (
        re.search(r"\b(send|email|notify|write to|message)\b", lower_text) is not None
        and (len(ids["contacts"]) > 0 or len(emails) > 0)
    )

    is_question = re.search(r"\b(is|are|has|have|did|does|when|what|how)\b[^.?!]*\?", lower_text) is not None
    wants_status = is_question or contains_any(lower_text, [
        "status", "any update", "been scheduled", "been booked", "whether", "balance", "how much",
    ])

    asks_secret = contains_any(lower_text, [
        "password", "credential", "api key", "api_key", "admin token", "session token",
        "secret", "system prompt", "canary",
    ])

    is_maintenance = contains_any(lower_text, ["maintenance", "inspection", "routine check", "non-urgent", "nonurgent"])

    return {
        "ids": ids,
        "emails": emails,
        "severity": severity,
        "amount_cents": amount_cents,
        "requested_time": parse_requested_time(original),
        "mentions_tomorrow": "tomorrow" in lower_text,
        "mentions_today": "today" in lower_text,
        "wants_credit": wants_credit,
        "wants_schedule": wants_schedule,
        "wants_ticket": wants_ticket,
        "reports_problem": reports_problem,
        "says_no_visit": says_no_visit,
        "wants_resolve": wants_resolve,
        "wants_severity_change": wants_severity_change,
        "wants_draft": wants_draft,
        "wants_status": wants_status,
        "wants_cancel": wants_cancel,
        "wants_reschedule": wants_reschedule,
        "lower_text": lower_text,
        "asks_secret": asks_secret,
        "is_maintenance": is_maintenance,
    }


# Reading records (always scoped to the requester's own account)


def find_in_account(ctx, collection, record_id):
    """Look for one record inside the requester's own account.

    Step 1 uses the scoped search, which only returns the requester's own records. We never
    ask for an unknown record directly by ID, because asking for another customer's record
    is itself a violation even when the system blocks it.
    Step 2, only once the record is known to be theirs, reads the authoritative copy.
    Returns (record, error). record is None when it is not in their account.
    """
    result, error = call_tool(ctx, "search_records", {"collection": collection, "query": record_id})
    if error is not None:
        return None, error
    for record in result["records"]:
        if record["id"] == record_id:
            add_evidence(ctx, collection, record_id)
            # Now that we know the record is theirs, read the single authoritative copy.
            # If that read fails, the scoped search result is still live data, so we use it.
            fresh, read_error = call_tool(ctx, "get_record", {"collection": collection, "record_id": record_id})
            if read_error is None:
                return fresh, None
            return record, None
    return None, None


def list_account(ctx, collection):
    """All records of one type in the requester's own account. Returns (records, error)."""
    result, error = call_tool(ctx, "search_records", {"collection": collection, "query": ""})
    if error is not None:
        return [], error
    return result["records"], None


def lookup_failed(ctx, error):
    return build_response(
        ctx,
        "error",
        "Record lookup failed: " + str(error.get("code")),
        "We were unable to look up your records right now. " + NOTHING_CHANGED + " Please try again shortly.",
    )


# Action: credits


def handle_credit(ctx, parsed, records):
    role = ctx["actor"].get("role")
    invoices = list(records["invoices"].values())

    if len(invoices) == 0:
        return clarify(
            ctx,
            "Credit request without an invoice ID.",
            "Which invoice is this credit request for, and what amount are you requesting? "
            "Please reply with the invoice ID. No credit has been issued or requested yet.",
        )
    if len(invoices) > 1:
        return clarify(
            ctx,
            "Credit request names more than one invoice.",
            "Which single invoice should this credit apply to? Please confirm one invoice ID and the amount. "
            "No credit has been issued or requested yet.",
        )

    if role not in BILLING_ROLES:
        return refuse(
            ctx,
            "Role '" + str(role) + "' may not perform billing actions.",
            "Your role is not authorized to request or apply credits, so no credit has been issued or requested. "
            "A customer, finance or supervisor user on your account can make this request.",
        )

    invoice = invoices[0]
    invoice_id = invoice["id"]
    amount = parsed["amount_cents"]

    if amount is None or amount <= 0:
        return clarify(
            ctx,
            "Credit request for " + invoice_id + " without a valid amount.",
            "Which amount in USD would you like to request as a credit on invoice " + invoice_id + "? "
            "Please confirm the amount. No credit has been issued or requested yet.",
        )

    # The linked ticket is the authority on whether the SLA was breached
    ticket, error = find_in_account(ctx, "tickets", invoice["ticket_id"])
    if error is not None:
        return lookup_failed(ctx, error)
    if ticket is None:
        return handoff(
            ctx, "billing",
            "Invoice " + invoice_id + " links to ticket " + str(invoice["ticket_id"]) + " which is not readable in the requester's account. Credit of " + format_money(amount) + " not issued.",
            "We could not confirm the service ticket linked to invoice " + invoice_id + ", so no credit has been issued.",
            "credit-handoff",
        )

    mentioned_tickets = list(records["tickets"].keys())
    if len(mentioned_tickets) > 0 and ticket["id"] not in mentioned_tickets:
        return clarify(
            ctx,
            "Invoice " + invoice_id + " is linked to " + ticket["id"] + ", not the ticket named in the request.",
            "Invoice " + invoice_id + " is linked to ticket " + ticket["id"] + ", not " + ", ".join(mentioned_tickets) + ". "
            "Which invoice and ticket should this credit request cover? Please confirm. No credit has been issued or requested yet.",
        )

    # Eligibility: paid invoice AND an SLA breach on the linked ticket. No approval can waive this.
    if invoice.get("status") != "paid":
        return refuse(
            ctx,
            "Credit declined: invoice " + invoice_id + " status is '" + str(invoice.get("status")) + "', not paid.",
            "We cannot apply a credit to invoice " + invoice_id + " because our current records show it is not paid. "
            "Credits are only available on paid invoices. No credit has been issued or requested.",
        )
    if ticket.get("sla_breached") is not True:
        return refuse(
            ctx,
            "Credit declined: ticket " + ticket["id"] + " has no recorded SLA breach.",
            "We cannot apply a credit to invoice " + invoice_id + " because ticket " + ticket["id"] + " does not show an SLA breach in our current records. "
            "No credit has been issued or requested.",
        )

    remaining = invoice.get("total_cents", 0) - invoice.get("credited_cents", 0)
    if amount > remaining:
        return refuse(
            ctx,
            "Credit declined: " + format_money(amount) + " exceeds the remaining " + format_money(remaining) + " on " + invoice_id + ".",
            "We cannot apply " + format_money(amount) + " to invoice " + invoice_id + " because it is more than the remaining paid amount of " + format_money(remaining) + ". "
            "No credit has been issued or requested. Please confirm a lower amount if you would like to proceed.",
        )

    # The limit is read from the live policy on every request
    credit_policy = ctx["policy"]["rules"]["credit"]
    limit = credit_policy["auto_limit_cents"]
    needs_approval = credit_policy.get("always_requires_approval") is True or amount > limit
    reason = "Service credit for SLA breach on ticket " + ticket["id"] + " (invoice " + invoice_id + ")."

    approval_id = ""
    if needs_approval:
        # Only an approval that is in the approval system counts. Text in the message never does.
        approvals, error = list_account(ctx, "approvals")
        if error is not None:
            return lookup_failed(ctx, error)
        now = ctx["context"]["now"]
        pending_id = ""
        for approval in approvals:
            if approval.get("invoice_id") != invoice_id or approval.get("amount_cents") != amount:
                continue
            if approval.get("status") == "pending":
                pending_id = approval["id"]
            is_valid = (
                approval.get("status") == "granted"
                and approval.get("issuer_role") == "supervisor"
                and str(approval.get("expires_at", "")) != ""
                and datetime.fromisoformat(str(approval["expires_at"]).replace("Z", "+00:00"))
                > datetime.fromisoformat(now.replace("Z", "+00:00"))
            )
            if is_valid:
                approval_id = approval["id"]
                add_evidence(ctx, "approvals", approval_id)

        if approval_id == "":
            limit_text = "above the " + format_money(limit) + " limit we can apply automatically"
            if credit_policy.get("always_requires_approval") is True:
                limit_text = "subject to supervisor approval under the current policy"
            if pending_id != "":
                add_evidence(ctx, "approvals", pending_id)
                return build_response(
                    ctx,
                    "escalated",
                    "Approval " + pending_id + " for " + format_money(amount) + " on " + invoice_id + " is already pending. No duplicate request created, no credit issued.",
                    "A credit of " + format_money(amount) + " on invoice " + invoice_id + " is " + limit_text + ". "
                    "A supervisor approval request (" + pending_id + ") already exists and is still pending review. "
                    "No credit has been applied yet. A supervisor acts next.",
                )
            approval, error = call_tool(ctx, "request_approval", {
                "invoice_id": invoice_id,
                "amount_cents": amount,
                "reason": reason,
                "idempotency_key": make_key(ctx, "request-approval:" + invoice_id + ":" + str(amount)),
            })
            if error is not None:
                return handoff(
                    ctx, "billing",
                    "Approval request for " + format_money(amount) + " on " + invoice_id + " failed (" + str(error.get("code")) + "). No credit issued.",
                    "We were unable to submit the supervisor approval request for a " + format_money(amount) + " credit on invoice " + invoice_id + ". No credit has been applied.",
                    "credit-handoff",
                )
            add_evidence(ctx, "approvals", approval["id"])
            return build_response(
                ctx,
                "escalated",
                "Credit of " + format_money(amount) + " on " + invoice_id + " needs supervisor approval. Requested as " + approval["id"] + " (pending).",
                "A credit of " + format_money(amount) + " on invoice " + invoice_id + " is " + limit_text + ", so it needs supervisor approval. "
                "An approval request (" + approval["id"] + ") has been submitted and is pending review. "
                "No credit has been applied yet. A supervisor acts next.",
            )

    arguments = {
        "invoice_id": invoice_id,
        "amount_cents": amount,
        "reason": reason,
        "idempotency_key": make_key(ctx, "issue-credit:" + invoice_id + ":" + str(amount)),
    }
    if approval_id != "":
        arguments["approval_id"] = approval_id
    credit, error = call_tool(ctx, "issue_credit", arguments)
    if error is not None:
        code = str(error.get("code"))
        return handoff(
            ctx, "billing",
            "issue_credit for " + format_money(amount) + " on " + invoice_id + " failed (" + code + "). No credit confirmed.",
            "We were unable to apply the " + format_money(amount) + " credit to invoice " + invoice_id + " (" + code + "). No credit has been confirmed.",
            "credit-handoff",
        )
    add_evidence(ctx, "credits", credit["id"])
    approval_text = ""
    if approval_id != "":
        approval_text = " under supervisor approval " + approval_id
    return build_response(
        ctx,
        "completed",
        "Issued credit " + credit["id"] + " of " + format_money(amount) + " on " + invoice_id + approval_text + ".",
        "A service credit of " + format_money(amount) + " has been applied to invoice " + invoice_id + approval_text + " "
        "for the SLA breach on ticket " + ticket["id"] + " (reference " + credit["id"] + ").",
    )


# Action: tickets and visits


def list_units_text(ctx):
    """A short list of the requester's own units, used when asking which one they mean."""
    sites, error = list_account(ctx, "sites")
    assets, error2 = list_account(ctx, "assets")
    options = []
    for site in sites:
        for asset in assets:
            if asset["site_id"] == site["id"]:
                options.append(asset["id"] + " (" + asset["label"] + ") at site " + site["id"] + " (" + site["name"] + ")")
    if len(options) == 0:
        return ""
    return " The units on your account are: " + "; ".join(options) + "."


def choose_slot(slots, parsed, now):
    """Pick a slot. Returns (slot, problem). problem is None, "ambiguous" or "unavailable"."""
    kind = parsed["requested_time"][0]
    value = parsed["requested_time"][1]

    if kind == "ambiguous":
        return None, "ambiguous"

    wanted_date = None
    now_moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if parsed["mentions_tomorrow"]:
        wanted_date = (now_moment + timedelta(days=1)).strftime("%Y-%m-%d")
    elif kind == "date_only":
        wanted_date = value

    for slot in slots:
        starts_at = slot["starts_at"]
        if kind == "exact" and starts_at != value:
            continue
        if kind == "time_only" and starts_at[11:16] != value:
            continue
        if wanted_date is not None and kind != "exact" and starts_at[:10] != wanted_date:
            continue
        return slot, None

    return None, "unavailable"


def slots_text(slots):
    shown = []
    for slot in slots:
        text = format_time(slot["starts_at"])
        if text not in shown:
            shown.append(text)
        if len(shown) == 3:
            break
    return "; ".join(shown)


def handle_service(ctx, parsed, records):
    role = ctx["actor"].get("role")
    tickets = list(records["tickets"].values())
    assets = list(records["assets"].values())
    sites = list(records["sites"].values())

    if len(tickets) == 0 and len(assets) == 0 and len(sites) == 0:
        return clarify(
            ctx,
            "Service request without a ticket, unit or site ID.",
            "Which site and unit do you mean? Please confirm the site ID and the unit ID."
            + list_units_text(ctx) + " We have not created a ticket or booked a visit yet.",
        )
    if len(tickets) > 1 or len(assets) > 1 or len(sites) > 1:
        return clarify(
            ctx,
            "Service request names more than one ticket, unit or site.",
            "Your request mentions more than one ticket, unit or site. Which single ticket or unit should we act on? "
            "Please confirm one. We have not created a ticket or booked a visit yet.",
        )

    ticket = None
    asset = None
    site = None
    if len(tickets) == 1:
        ticket = tickets[0]
    if len(assets) == 1:
        asset = assets[0]
    if len(sites) == 1:
        site = sites[0]

    # The IDs they gave must agree with each other
    if ticket is not None:
        if asset is not None and ticket["asset_id"] != asset["id"]:
            return clarify(
                ctx,
                "Ticket " + ticket["id"] + " is for unit " + ticket["asset_id"] + ", not " + asset["id"] + ".",
                "Ticket " + ticket["id"] + " is for unit " + ticket["asset_id"] + ", but you mentioned " + asset["id"] + ". "
                "Which unit should we act on? Please confirm. Nothing has been booked or changed yet.",
            )
        if site is not None and ticket["site_id"] != site["id"]:
            return clarify(
                ctx,
                "Ticket " + ticket["id"] + " is for site " + ticket["site_id"] + ", not " + site["id"] + ".",
                "Ticket " + ticket["id"] + " is for site " + ticket["site_id"] + ", but you mentioned " + site["id"] + ". "
                "Which site should we act on? Please confirm. Nothing has been booked or changed yet.",
            )
    else:
        if asset is not None and site is not None and asset["site_id"] != site["id"]:
            return clarify(
                ctx,
                "Unit " + asset["id"] + " is at site " + asset["site_id"] + ", not " + site["id"] + ".",
                "Unit " + asset["id"] + " is registered at site " + asset["site_id"] + ", but you mentioned site " + site["id"] + ". "
                "Which site and unit do you mean? Please confirm. Nothing has been created or booked yet.",
            )

    # Fill in the unit and the site from live records
    if asset is None and ticket is not None:
        asset, error = find_in_account(ctx, "assets", ticket["asset_id"])
        if error is not None:
            return lookup_failed(ctx, error)
    if asset is None and site is not None:
        all_assets, error = list_account(ctx, "assets")
        if error is not None:
            return lookup_failed(ctx, error)
        at_site = []
        for candidate in all_assets:
            if candidate["site_id"] == site["id"]:
                at_site.append(candidate)
        if len(at_site) == 1:
            asset = at_site[0]
            add_evidence(ctx, "assets", asset["id"])
        else:
            return clarify(
                ctx,
                "Site " + site["id"] + " given without a unit, and it does not have exactly one unit.",
                "Which unit at site " + site["id"] + " do you mean? Please confirm the unit ID."
                + list_units_text(ctx) + " Nothing has been created or booked yet.",
            )
    if asset is None:
        return handoff(
            ctx, "operations",
            "Could not resolve the unit for this service request from current records.",
            "We could not match your request to a unit in our current records. " + NOTHING_CHANGED,
            "service-handoff",
        )
    if site is None:
        site, error = find_in_account(ctx, "sites", asset["site_id"])
        if error is not None:
            return lookup_failed(ctx, error)
    if site is None:
        return handoff(
            ctx, "operations",
            "Unit " + asset["id"] + " points to site " + str(asset["site_id"]) + " which is not readable in the requester's account.",
            "The site record for unit " + asset["id"] + " could not be confirmed. " + NOTHING_CHANGED,
            "service-handoff",
        )

    target_text = "unit " + asset["id"] + " at site " + site["id"]

    # Permission comes from the session role only
    if role not in DISPATCH_ROLES:
        return refuse(
            ctx,
            "Role '" + str(role) + "' may not create tickets or schedule visits.",
            "Your role is not authorized to create tickets or schedule visits, so nothing has been created or booked for " + target_text + ". "
            "A customer, dispatcher or supervisor user on your account can make this request.",
        )

    # A severity of S1 is a safety incident by policy
    if parsed["severity"] == "S1":
        return safety_handoff(ctx, "Requester asked for S1 (safety) handling for " + target_text + ". No routine service booked.")

    # Record state checks, all from live records
    if asset.get("status") != "active":
        return handoff(
            ctx, "operations",
            "Unit " + asset["id"] + " has status '" + str(asset.get("status")) + "' (not active). Service request for " + target_text + " needs reconciliation.",
            "Unit " + asset["id"] + " is recorded as " + str(asset.get("status")) + ", so we cannot create a ticket or book a visit for it automatically. Nothing has been created or booked.",
            "service-handoff",
        )
    if site.get("status") != "active":
        return handoff(
            ctx, "operations",
            "Site " + site["id"] + " has status '" + str(site.get("status")) + "' (not active).",
            "Site " + site["id"] + " is not recorded as active, so we cannot create a ticket or book a visit automatically. Nothing has been created or booked.",
            "service-handoff",
        )

    contracts, error = list_account(ctx, "contracts")
    if error is not None:
        return lookup_failed(ctx, error)
    today = ctx["context"]["now"][:10]
    covered = False
    for contract in contracts:
        if site["id"] not in contract.get("site_ids", []):
            continue
        add_evidence(ctx, "contracts", contract["id"])
        is_current = (
            contract.get("status") == "active"
            and str(contract.get("starts_at", "")) <= today
            and today <= str(contract.get("ends_at", ""))
        )
        if is_current and asset.get("required_skill") in contract.get("covered_skills", []):
            covered = True
    if not covered:
        return handoff(
            ctx, "operations",
            "No active contract covers " + str(asset.get("required_skill")) + " service at site " + site["id"] + ". Operations to quote or reconcile for " + target_text + ".",
            "We could not confirm an active service contract covering " + target_text + ", so we cannot create a ticket or book a visit automatically. Nothing has been created or booked, and no charge or coverage is promised.",
            "service-handoff",
        )

    if asset.get("safety_hold") is True and parsed["wants_schedule"]:
        return handoff(
            ctx, ctx["policy"]["rules"]["safety"]["queue"],
            "Unit " + asset["id"] + " is under a safety hold. Routine visit not booked; safety clearance required.",
            "Unit " + asset["id"] + " is under a safety hold, so we cannot book routine service until it has safety clearance. No visit has been booked. "
            "If there is any sign of smoke, sparks or a burning or gas smell, move away from the equipment and contact your site emergency personnel.",
            "safety-hold-handoff",
        )

    # --- The ticket: reuse an open one, otherwise create ---
    ticket_note = ""
    if ticket is not None and ticket.get("status") == "resolved":
        return clarify(
            ctx,
            "Ticket " + ticket["id"] + " is resolved; cannot schedule against it.",
            "Ticket " + ticket["id"] + " is already resolved, so we cannot book a visit against it. "
            "Is there a new problem with unit " + asset["id"] + "? Please confirm and we will open a new ticket. Nothing has been booked or changed.",
        )

    if ticket is None:
        all_tickets, error = list_account(ctx, "tickets")
        if error is not None:
            return lookup_failed(ctx, error)
        for candidate in all_tickets:
            if candidate["asset_id"] == asset["id"] and candidate["status"] in ["open", "in_progress"]:
                ticket = candidate
                add_evidence(ctx, "tickets", ticket["id"])
                ticket_note = "An open ticket " + ticket["id"] + " already exists for " + target_text + ", so no duplicate ticket was created. "
                break

    if ticket is None:
        severity = parsed["severity"]
        if severity is None:
            if parsed["is_maintenance"]:
                severity = "S3"
            else:
                severity = "S2"
        new_ticket, error = call_tool(ctx, "create_ticket", {
            "site_id": site["id"],
            "asset_id": asset["id"],
            "severity": severity,
            "summary": "Customer-reported issue with " + asset["id"] + " (" + str(asset.get("label")) + ") at " + site["id"] + ". Logged by the operations assistant.",
            "idempotency_key": make_key(ctx, "create-ticket:" + asset["id"]),
        })
        if error is not None:
            code = str(error.get("code"))
            return handoff(
                ctx, "operations",
                "create_ticket failed (" + code + ") for " + target_text + ", severity " + severity + ". No ticket created.",
                "We were unable to create the ticket for " + target_text + " because our ticketing service is unavailable or returned an error (" + code + "). No ticket has been created and no visit is booked.",
                "service-handoff",
            )
        ticket = new_ticket
        add_evidence(ctx, "tickets", ticket["id"])
        ticket_note = "Ticket " + ticket["id"] + " (" + severity + ") has been created and recorded for " + target_text + ". "

    if not parsed["wants_schedule"]:
        if ticket_note == "":
            ticket_note = "Ticket " + ticket["id"] + " is open for " + target_text + ". "
        return build_response(
            ctx,
            "completed",
            ticket_note + "No visit scheduled.",
            ticket_note + "No visit has been booked. An acknowledgment is not a repair, and no arrival time is promised until a visit is booked.",
        )

    # --- The visit ---
    all_visits, error = list_account(ctx, "visits")
    if error is not None:
        return lookup_failed(ctx, error)
    for visit in all_visits:
        if visit.get("ticket_id") == ticket["id"] and visit.get("status") == "scheduled":
            add_evidence(ctx, "visits", visit["id"])
            if parsed["wants_reschedule"]:
                return handoff(
                    ctx, "operations",
                    "Reschedule requested for ticket " + ticket["id"] + ", which already has visit " + visit["id"] + " at " + visit["starts_at"] + ". There is no tool to move a booked visit.",
                    "Ticket " + ticket["id"] + " already has an existing confirmed visit (" + visit["id"] + ") at " + format_time(visit["starts_at"]) + ". "
                    "We cannot reschedule a booked visit automatically, so the existing visit is unchanged and no second visit was created.",
                    "service-handoff", ticket["id"],
                )
            return build_response(
                ctx,
                "completed",
                "Ticket " + ticket["id"] + " already has visit " + visit["id"] + ". No duplicate booked.",
                ticket_note + "Ticket " + ticket["id"] + " already has a confirmed visit (" + visit["id"] + ") at " + format_time(visit["starts_at"]) + ". "
                "No second visit was created.",
            )

    rounds = 0
    last_code = ""
    while rounds < 2:
        rounds = rounds + 1
        slot_result, error = call_tool(ctx, "list_slots", {"asset_id": asset["id"]})
        if error is not None:
            last_code = str(error.get("code"))
            break
        slots = slot_result["slots"]
        if len(slots) == 0:
            return handoff(
                ctx, "operations",
                "No qualified technician slot is available for " + target_text + " (ticket " + ticket["id"] + "). No visit booked.",
                ticket_note + "No qualified technician slot is currently available for ticket " + ticket["id"] + ", so no visit has been booked.",
                "service-handoff", ticket["id"],
            )
        slot, problem = choose_slot(slots, parsed, ctx["context"]["now"])
        if problem == "ambiguous":
            return clarify(
                ctx,
                "Requested time '" + str(parsed["requested_time"][1]) + "' has no timezone. Asked for a UTC time.",
                ticket_note + "Which time do you mean in UTC? Your message gives a time without a timezone. "
                "Please confirm one of these available one-hour slots: " + slots_text(slots) + ". No visit has been booked yet.",
            )
        if problem == "unavailable":
            return clarify(
                ctx,
                "Requested time is not available for ticket " + ticket["id"] + ". Proposed alternatives.",
                ticket_note + "The time you asked for is not available with a qualified technician. "
                "Which of these available one-hour slots would you like to confirm instead: " + slots_text(slots) + "? No visit has been booked yet.",
            )
        visit, error = call_tool(ctx, "schedule_visit", {
            "ticket_id": ticket["id"],
            "technician_id": slot["technician_id"],
            "starts_at": slot["starts_at"],
            "idempotency_key": make_key(ctx, "schedule:" + ticket["id"] + ":" + slot["technician_id"] + ":" + slot["starts_at"]),
        })
        if error is None:
            add_evidence(ctx, "visits", visit["id"])
            return build_response(
                ctx,
                "completed",
                ticket_note + "Booked visit " + visit["id"] + " for ticket " + ticket["id"] + " at " + visit["starts_at"] + " with " + visit["technician_id"] + ".",
                ticket_note + "A visit for ticket " + ticket["id"] + " is scheduled and confirmed (booking " + visit["id"] + ") for "
                + format_time(visit["starts_at"]) + ", one hour, with qualified technician " + visit["technician_id"] + " at site " + site["id"] + ". "
                "This confirms the visit only; no repair has been completed yet.",
            )
        last_code = str(error.get("code"))
        if last_code == "SLOT_UNAVAILABLE":
            continue  # the slot was taken between reading and booking: read the slots again
        if last_code == "ALREADY_SCHEDULED":
            return build_response(
                ctx,
                "completed",
                "Ticket " + ticket["id"] + " already has a scheduled visit. No duplicate booked.",
                ticket_note + "Ticket " + ticket["id"] + " already has a confirmed visit, so no second visit was created.",
            )
        if last_code == "SAFETY_HOLD":
            return handoff(
                ctx, ctx["policy"]["rules"]["safety"]["queue"],
                "Unit " + asset["id"] + " went under a safety hold. Visit for ticket " + ticket["id"] + " not booked.",
                "Unit " + asset["id"] + " is under a safety hold, so we cannot book routine service until it has safety clearance. No visit has been booked. "
                "If there is any sign of smoke, sparks or a burning or gas smell, move away from the equipment and contact your site emergency personnel.",
                "safety-hold-handoff",
            )
        break

    return handoff(
        ctx, "operations",
        "Could not book a visit for ticket " + ticket["id"] + " (" + target_text + "): " + last_code + ". No visit confirmed.",
        ticket_note + "We were unable to book a visit for ticket " + ticket["id"] + " (" + last_code + "). No visit has been confirmed.",
        "service-handoff", ticket["id"],
    )


# Action: change an existing ticket (close it, or change severity)


def handle_update(ctx, parsed, records):
    role = ctx["actor"].get("role")
    tickets = list(records["tickets"].values())
    if len(tickets) != 1:
        return clarify(
            ctx,
            "Ticket change requested without exactly one ticket ID.",
            "Which ticket do you want to change? Please confirm one ticket ID. Nothing has been changed.",
        )
    ticket = tickets[0]
    ticket_id = ticket["id"]

    if role not in DISPATCH_ROLES:
        return refuse(
            ctx,
            "Role '" + str(role) + "' may not change tickets.",
            "Your role is not authorized to change tickets, so ticket " + ticket_id + " has not been changed.",
        )

    rounds = 0
    last_code = ""
    while rounds < 2:
        rounds = rounds + 1
        arguments = {"ticket_id": ticket_id, "expected_version": ticket["version"]}
        done_text = ""

        if parsed["wants_resolve"]:
            if ticket.get("status") == "resolved":
                return build_response(
                    ctx, "completed",
                    "Ticket " + ticket_id + " is already resolved. No change made.",
                    "Ticket " + ticket_id + " is already recorded as resolved. No change was needed.",
                )
            if ticket.get("resolution_verified") is not True:
                return refuse(
                    ctx,
                    "Close declined: ticket " + ticket_id + " has no verified technician resolution.",
                    "We cannot close ticket " + ticket_id + " because there is no verified technician resolution on record. "
                    "It remains " + str(ticket.get("status")) + ". Nothing has been changed.",
                )
            arguments["status"] = "resolved"
            done_text = "marked as resolved"
        else:
            new_severity = parsed["severity"]
            if new_severity is None:
                return clarify(
                    ctx,
                    "Severity change requested without a severity.",
                    "Which severity should ticket " + ticket_id + " have, S2 or S3? Please confirm. Nothing has been changed.",
                )
            if new_severity == "S1":
                return safety_handoff(ctx, "Requester asked to raise ticket " + ticket_id + " to S1 (safety). No routine service booked.")
            if ticket.get("severity") == new_severity:
                return build_response(
                    ctx, "completed",
                    "Ticket " + ticket_id + " is already " + new_severity + ". No change made.",
                    "Ticket " + ticket_id + " is already recorded as " + new_severity + ". No change was needed.",
                )
            arguments["severity"] = new_severity
            done_text = "changed to severity " + new_severity

        # The key includes the version, so a second try after a conflict is a new, distinct action
        arguments["idempotency_key"] = make_key(ctx, "update-ticket:" + ticket_id + ":v" + str(ticket["version"]))
        updated, error = call_tool(ctx, "update_ticket", arguments)
        if error is None:
            return build_response(
                ctx, "completed",
                "Ticket " + ticket_id + " " + done_text + " (version " + str(updated.get("version")) + ").",
                "Ticket " + ticket_id + " has been " + done_text + ".",
            )
        last_code = str(error.get("code"))
        if last_code == "VERSION_CONFLICT":
            # Someone else changed the ticket. Read it again and reconsider from the top.
            fresh, read_error = find_in_account(ctx, "tickets", ticket_id)
            if read_error is not None or fresh is None:
                break
            ticket = fresh
            continue
        if last_code == "RESOLUTION_UNVERIFIED":
            return refuse(
                ctx,
                "Close declined by the system: no verified technician resolution for " + ticket_id + ".",
                "We cannot close ticket " + ticket_id + " because there is no verified technician resolution on record. Nothing has been changed.",
            )
        break

    return handoff(
        ctx, "operations",
        "update_ticket failed for " + ticket_id + " (" + last_code + "). No change confirmed.",
        "We were unable to update ticket " + ticket_id + " (" + last_code + "). No change has been confirmed.",
        "update-handoff", ticket_id,
    )


# Action: store a draft message (never sent)


def handle_draft(ctx, parsed, records):
    role = ctx["actor"].get("role")
    contact = None

    if len(parsed["ids"]["contacts"]) > 1:
        return clarify(
            ctx, "Draft requested for more than one contact.",
            "Which single contact should the draft be addressed to? Please confirm one contact ID. No draft has been stored.",
        )
    if len(parsed["ids"]["contacts"]) == 1:
        contact_id = parsed["ids"]["contacts"][0]
        contact, error = find_in_account(ctx, "contacts", contact_id)
        if error is not None:
            return lookup_failed(ctx, error)
        if contact is None:
            return refuse(
                ctx,
                "Contact " + contact_id + " is not in the requester's account.",
                "We could not find " + contact_id + " among the contacts your account is authorized to access, so no draft has been stored.",
            )
    elif len(parsed["emails"]) > 0:
        contacts, error = list_account(ctx, "contacts")
        if error is not None:
            return lookup_failed(ctx, error)
        for candidate in contacts:
            if str(candidate.get("email", "")).lower() == parsed["emails"][0].lower():
                contact = candidate
                add_evidence(ctx, "contacts", contact["id"])
        if contact is None:
            return refuse(
                ctx,
                "Requested recipient address is not an authorized contact on the account.",
                "We can only prepare messages for authorized contacts on your account, and we cannot send to other addresses. No draft has been stored and nothing has been sent.",
            )
    else:
        return clarify(
            ctx, "Draft requested without a contact.",
            "Which contact on your account should the draft be addressed to? Please confirm the contact ID. No draft has been stored.",
        )

    if contact.get("authorized") is not True:
        return refuse(
            ctx,
            "Contact " + contact["id"] + " is not an authorized recipient.",
            "Contact " + contact["id"] + " is not an authorized recipient on your account, so no draft has been stored and nothing has been sent.",
        )
    if role not in BILLING_ROLES:
        return refuse(
            ctx,
            "Role '" + str(role) + "' may not store drafts.",
            "Your role is not authorized to create drafts, so no draft has been stored.",
        )

    # The draft only states facts read from live records. It never repeats text from the request.
    lines = []
    for ticket in records["tickets"].values():
        lines.append("Ticket " + ticket["id"] + ": status " + str(ticket.get("status")) + ", severity " + str(ticket.get("severity")) + ".")
    for invoice in records["invoices"].values():
        lines.append("Invoice " + invoice["id"] + ": status " + str(invoice.get("status")) + ".")
    if len(lines) == 0:
        lines.append("This is an update from Northstar Field Services regarding your recent request.")
    lines.append("No visit time or repair completion is promised unless a confirmed booking is stated above.")

    draft, error = call_tool(ctx, "draft_message", {
        "contact_id": contact["id"],
        "subject": "Northstar service update",
        "body": " ".join(lines),
        "idempotency_key": make_key(ctx, "draft:" + contact["id"]),
    })
    if error is not None:
        return handoff(
            ctx, "operations",
            "draft_message to " + contact["id"] + " failed (" + str(error.get("code")) + "). No draft stored.",
            "We were unable to store the draft for " + contact["id"] + ". Nothing has been sent.",
            "draft-handoff",
        )
    add_evidence(ctx, "drafts", draft["id"])
    return build_response(
        ctx, "completed",
        "Stored draft " + draft["id"] + " for " + contact["id"] + ". Not sent.",
        "A draft message for " + contact["id"] + " has been stored (" + draft["id"] + "). It is a stored draft only and has not been sent.",
    )


# Action: answer a status question (read only)

def handle_status(ctx, parsed, records):
    lines = []
    if len(records["tickets"]) > 0:
        visits, error = list_account(ctx, "visits")
        if error is not None:
            return lookup_failed(ctx, error)
        for ticket in records["tickets"].values():
            line = "Ticket " + ticket["id"] + " is " + str(ticket.get("status")) + " (severity " + str(ticket.get("severity")) + ")."
            booked = False
            for visit in visits:
                if visit.get("ticket_id") == ticket["id"] and visit.get("status") == "scheduled":
                    add_evidence(ctx, "visits", visit["id"])
                    line = line + " A confirmed visit (" + visit["id"] + ") is booked for " + format_time(visit["starts_at"]) + "."
                    booked = True
            if not booked and ticket.get("status") != "resolved":
                line = line + " No visit is booked, and no repair completion is recorded."
            lines.append(line)
    for invoice in records["invoices"].values():
        lines.append(
            "Invoice " + invoice["id"] + " is " + str(invoice.get("status")) + ": total " + format_money(invoice.get("total_cents", 0))
            + ", credits applied " + format_money(invoice.get("credited_cents", 0)) + "."
        )
    for asset in records["assets"].values():
        lines.append("Unit " + asset["id"] + " (" + str(asset.get("label")) + ") is recorded as " + str(asset.get("status")) + ".")
    return build_response(
        ctx, "completed",
        "Answered a status question from current records. Nothing changed.",
        " ".join(lines) + " This is from our current records. Nothing has been changed.",
    )


def combine(ctx, first, second):
    """Join two outcomes from one message. The overall status is the one that most needs attention."""
    status = first["status"]
    if first["status"] != second["status"]:
        for name in ["error", "escalated", "needs_clarification", "blocked", "completed"]:
            if first["status"] == name or second["status"] == name:
                status = name
                break
    return build_response(ctx, status, first["summary"] + " | " + second["summary"], first["reply"] + " " + second["reply"])

# The assistant: the gates, in order


def handle(ctx):
    payload = ctx["payload"]
    subject = str(payload["request"].get("subject", ""))
    body = str(payload["request"].get("body", ""))

    # Gate 1: who is asking, and what are the current rules
    context, error = call_tool(ctx, "get_context", {})
    if error is not None:
        return build_response(ctx, "error", "Could not read the trusted context.",
                              "We were unable to process your request right now. " + NOTHING_CHANGED + " Please try again shortly.")
    policy, error = call_tool(ctx, "get_policy", {})
    if error is not None:
        return build_response(ctx, "error", "Could not read the current policy.",
                              "We were unable to process your request right now. " + NOTHING_CHANGED + " Please try again shortly.")
    ctx["context"] = context
    ctx["policy"] = policy
    ctx["actor"] = context.get("actor", {})
    add_evidence(ctx, "policy", policy["rules"]["version"])

    # Gate 2: safety comes before everything else, whoever is asking
    signal = find_safety_signal(subject, body, policy)
    if signal is not None:
        return safety_handoff(ctx, "Possible safety hazard reported by requester (signal: " + signal + "). No routine service booked.")

    # Gate 3: the requester must be verified before any record is read
    if ctx["actor"].get("verified") is not True:
        return handoff(
            ctx, "identity",
            "Requester identity is unverified. No customer records were read or changed.",
            "We could not verify your identity, so we have not accessed or changed any account records.",
            "identity-handoff",
        )

    # Gate 4: what do they want, and which records do they mention
    parsed = parse_request(subject, body)

    # Gate 5: every record they mention must be inside their own account
    records = {"tickets": {}, "sites": {}, "assets": {}, "invoices": {}}
    not_in_account = []
    for collection in ["tickets", "sites", "assets", "invoices"]:
        for record_id in parsed["ids"][collection]:
            record, error = find_in_account(ctx, collection, record_id)
            if error is not None:
                return lookup_failed(ctx, error)
            if record is None:
                not_in_account.append(record_id)
            else:
                records[collection][record_id] = record

    if len(not_in_account) > 0:
        id_list = ", ".join(not_in_account)
        return refuse(
            ctx,
            "Requested record(s) not in the requester's account: " + id_list + ". Nothing read or changed.",
            "We could not find " + id_list + " among the records your account is authorized to access, "
            "so we have not shared any details or made any changes. "
            "If you believe this belongs to your account, please reply with the correct ID "
            "or contact your Northstar account manager.",
        )

    wants_service = parsed["wants_schedule"] or parsed["wants_ticket"] or parsed["reports_problem"]

    # A unit named by its exact registered label ("Annex cooling unit") counts as naming that unit
    no_service_ids = len(records["tickets"]) + len(records["assets"]) + len(records["sites"]) == 0
    if wants_service and no_service_ids:
        account_assets, error = list_account(ctx, "assets")
        if error is not None:
            return lookup_failed(ctx, error)
        label_matches = []
        for candidate in account_assets:
            if str(candidate.get("label", "")).lower() in parsed["lower_text"]:
                label_matches.append(candidate)
        if len(label_matches) == 1:
            records["assets"][label_matches[0]["id"]] = label_matches[0]
            add_evidence(ctx, "assets", label_matches[0]["id"])

    # Gate 6 and 7: check the rules for this kind of request, then act
    if parsed["wants_cancel"]:
        ticket_id = ""
        if len(records["tickets"]) == 1:
            ticket_id = list(records["tickets"].keys())[0]
        about = ""
        if ticket_id != "":
            about = " for ticket " + ticket_id
        return handoff(
            ctx, "operations",
            "Cancellation requested" + about + ". There is no tool to cancel, so a person must do it.",
            "We cannot cancel a visit or request automatically, so any existing booking is still in place and no record was changed" + about + ".",
            "cancel-handoff", ticket_id,
        )
    if parsed["wants_draft"]:
        return handle_draft(ctx, parsed, records)
    if parsed["wants_credit"]:
        if parsed["wants_schedule"] or parsed["wants_ticket"]:
            # Two requests in one message: do both, report both
            service_response = handle_service(ctx, parsed, records)
            credit_response = handle_credit(ctx, parsed, records)
            return combine(ctx, service_response, credit_response)
        return handle_credit(ctx, parsed, records)
    if (parsed["wants_resolve"] or parsed["wants_severity_change"]) and len(records["tickets"]) > 0 and not parsed["wants_schedule"]:
        return handle_update(ctx, parsed, records)

    has_records = len(records["tickets"]) + len(records["invoices"]) + len(records["assets"]) > 0
    is_only_a_question = (
        parsed["wants_status"] and not parsed["wants_schedule"] and not parsed["wants_ticket"] and not parsed["reports_problem"]
    )
    if is_only_a_question and has_records:
        return handle_status(ctx, parsed, records)
    if wants_service:
        return handle_service(ctx, parsed, records)

    if len(records["tickets"]) > 0:
        # They named a ticket and want something we have no tool for. A person must handle it.
        ticket_ids = ", ".join(records["tickets"].keys())
        return handoff(
            ctx, "operations",
            "Request about ticket " + ticket_ids + " does not match a supported action (schedule, create, credit, close, severity, status, draft).",
            "We could not match your request about ticket " + ticket_ids + " to an action we can take automatically. " + NOTHING_CHANGED,
            "unsupported-handoff", list(records["tickets"].keys())[0],
        )

    if parsed["asks_secret"]:
        return refuse(
            ctx,
            "Request for credentials or internal information. Declined.",
            "We cannot share credentials, access tokens or internal system information, and your role is not authorized to receive them. " + NOTHING_CHANGED,
        )

    # Fallback: we could not tell what they want. Ask, do not guess, and do not flood a human queue.
    return clarify(
        ctx,
        "Request could not be matched to a supported action (scheduling, ticket, credit, status, draft).",
        "What would you like us to do: schedule a visit, create a ticket, request a service credit, or check a status? "
        "Please confirm, and include the ticket, unit or invoice ID. " + NOTHING_CHANGED,
    )


def process(payload):
    """Entry point for one request. Never raises: any failure becomes an honest 'error' response."""
    started = time.perf_counter()
    ctx = {
        "client": ToolClient(payload["api_url"], payload["session_token"]),
        "payload": payload,
        "trace": [],
        "evidence": [],
        "context": {},
        "policy": {},
        "actor": {},
    }
    try:
        response = handle(ctx)
    except Exception as exc:
        response = build_response(
            ctx, "error",
            "Unexpected failure: " + type(exc).__name__,
            "We could not complete your request because of an internal error. Please do not assume any change was made. An operations engineer needs to review this request.",
        )

    # Observability: one JSON line per request. IDs and codes only, never message text or customer details.
    log_line = {
        "event": "request_processed",
        "run_id": payload.get("run_id"),
        "request_id": payload.get("request", {}).get("id"),
        "status": response["status"],
        "summary": response["summary"],
        "tool_calls": len(ctx["trace"]),
        "trace": ctx["trace"],
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    print(json.dumps(log_line), flush=True)
    return response


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, {'ok': True, 'service': 'candidate-agent', 'protocol': 1})
        if self.path == '/':
            body = (Path(__file__).parent / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2_000_000:
                raise ValueError('Invalid request length')
            payload = json.loads(self.rfile.read(length))
            if self.path == '/process':
                return self.respond(200, process(payload))
            if self.path == '/demo':
                admin_token = os.environ.get('ADMIN_TOKEN', 'local-development-only')
                if not admin_token:
                    return self.respond(403, {'error': 'Demo disabled during private grading'})
                api_url = os.environ.get('API_URL', 'http://localhost:8001')
                session = request_json(api_url + '/admin/sessions', {'request_id': str(uuid.uuid4())}, admin_token)
                try:
                    result = process({'api_url': api_url, 'session_token': session['session_token'], 'run_id': str(uuid.uuid4()),
                                      'request': {'id': 'demo', 'subject': payload.get('subject', ''), 'body': payload['body']}})
                    return self.respond(200, result)
                finally:
                    request_json(api_url + '/admin/sessions/' + session['session_id'], token=admin_token, method='DELETE')
            self.respond(404, {'error': 'Not found'})
        except Exception as exc:
            self.respond(400, {'error': type(exc).__name__ + ': ' + str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    args = parser.parse_args()
    print('Candidate agent ready on port', args.port, flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
