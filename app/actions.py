"""Actions shared by the dashboard and the Copilot, so both do exactly the same thing."""
from . import db, enrich, llm, outreach, research


async def approve_lead(lead_id: int, note: str | None = None, channels: list[str] | None = None) -> dict:
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        return {"error": "no such lead"}
    if not lead["enriched_at"]:
        await enrich.enrich_lead(lead_id)
    researched = None
    if lead["research"] is None and llm.available():  # find the right people before writing to them
        researched = await research.research_lead(lead_id)
    db.x("UPDATE leads SET status='approved', note=COALESCE(?,note), updated_at=? WHERE id=?", (note, db.now(), lead_id))
    seq = await outreach.draft_sequence(lead_id, channels)
    return {"sequence": seq, "research": researched}


def reject_lead(lead_id: int, note: str | None = None) -> dict:
    if not db.one("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
        return {"error": "no such lead"}
    outreach.stop_sequence(lead_id, "lead rejected")
    db.x("UPDATE leads SET status='rejected', note=COALESCE(?,note), updated_at=? WHERE id=?", (note, db.now(), lead_id))
    return {"ok": True}
