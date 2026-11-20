"""The Writer agent: writes a lead's sequence from the evidence, and has its drafts checked by code before they are saved.

It may read up to two of the pages behind the lead's signals to find one specific, checkable detail. When it saves,
the drafts are checked against the house rules (lengths, no exclamation marks or em dashes, no stock openers, the
LinkedIn note limit); a draft that fails is refused with the reasons, and the agent rewrites it.
"""
import re
from typing import TypedDict

from anthropic import beta_async_tool

from . import agentkit
from . import brain as brain_mod
from . import config, insights


class Email(TypedDict):
    subject: str
    body: str


class CallScript(TypedDict):
    opener: str
    reason: str
    questions: list[str]
    voicemail: str


WORD_LIMITS = (100, 70, 60)  # a little room over the 90 / 60 / 50 asked for
STOCK = (("!", "an exclamation mark"), ("—", "an em dash"), ("hope this finds you", '"I hope this finds you well"'),
         ("quick question", '"quick question"'), ("just following up", '"just following up"'), ("circle back", '"circle back"'))

CHECKLIST = """Before calling save_sequence, check every message yourself:
- It opens with the real signal, stated accurately. Nothing about the prospect is invented.
- Email 1 under 90 words, email 2 under 60, email 3 under 50. Subjects under 6 words.
- No exclamation marks, no emoji, no em dashes, no "I hope this finds you well", no "quick question".
- The LinkedIn note is at most {note_max} characters.
- Facts about the founder's company come only from the company profile.
save_sequence also checks these and refuses drafts that fail, telling you what to fix."""


def check(emails: list, note: str, note_max: int) -> list[str]:
    problems = []
    if len(emails) != 3:
        problems.append(f"there must be exactly 3 emails, not {len(emails)}")
    for i, em in enumerate(emails[:3], start=1):
        words = len(re.findall(r"\S+", em.get("body", "")))
        if words > WORD_LIMITS[i - 1]:
            problems.append(f"email {i} is {words} words; keep it under {WORD_LIMITS[i - 1] - 10}")
        if len((em.get("subject") or "").split()) > 7:
            problems.append(f"email {i}'s subject is too long")
        text = f"{em.get('subject', '')} {em.get('body', '')}".lower()
        problems += [f"email {i} contains {label}" for token, label in STOCK if token in text]
    if len(note or "") > note_max:
        problems.append(f"the LinkedIn note is {len(note)} characters; the limit is {note_max}")
    problems += [f"the LinkedIn note contains {label}" for token, label in STOCK if token in (note or "").lower()]
    return problems


async def write(lead: dict, contact: dict | None, signals: list[dict], brain: dict, angle: str) -> dict | None:
    """Returns the sequence in the same shape as the single-call writer, or None to let it fall back."""
    from . import outreach
    note_max = outreach.LINKEDIN_NOTE_MAX
    out: dict = {}

    @beta_async_tool
    async def save_sequence(emails: list[Email], linkedin_note: str, linkedin_followup: str, call: CallScript) -> str:
        """Save the finished sequence. Call this once you have checked every message against the checklist.

        Args:
            emails: Exactly 3 emails in order. Emails 2 and 3 reuse email 1's subject with "Re: " in front.
            linkedin_note: The connection request note.
            linkedin_followup: The message to send after they accept, under 60 words.
            call: The call script: opener, reason, 2 or 3 questions, and a voicemail under 35 words.
        """
        problems = check(emails, linkedin_note, note_max)
        if problems:
            return "Not saved. Fix these and call save_sequence again: " + "; ".join(problems) + "."
        out.update(emails=emails, linkedin_note=linkedin_note, linkedin_followup=linkedin_followup, call=call)
        return "Saved. You are done."

    urls = [s["url"] for s in signals[:3] if (s.get("url") or "").startswith("http")][:2]
    research = lead.get("research") or {}
    for p in research.get("people", []):
        urls += [u for u in p.get("evidence", []) if u not in urls][:1]
    urls = urls[:3]
    system = (outreach.SEQ_SYSTEM + "\n\n# Opening angle for email 1 and the LinkedIn note (an experiment; follow it)\n"
              + insights.ANGLES[angle] + "\n\n# Company\n" + brain_mod.summary(brain) + "\n\n"
              + CHECKLIST.format(note_max=note_max))
    prompt = outreach._context(lead, contact, signals, brain)
    if urls:
        prompt += ("\n\n# Pages behind these signals\nYou may read up to two with web_fetch to find one specific, "
                   "checkable detail worth mentioning. Skip it if the signal says enough.\n" + "\n".join(urls))
    result = await agentkit.run("writer", system=system, subject=lead.get("company_name") or "",
                                messages=[{"role": "user", "content": prompt + "\n\nWrite the sequence and save it."}],
                                tools=[save_sequence], web=agentkit.web_tools(config.LLM_MODEL, 0, 2 if urls else 0),
                                max_turns=8)
    if out:
        agentkit.note(result.run_id, f"Wrote and checked the sequence for {lead.get('company_name') or 'a lead'}.")
    return out or None
