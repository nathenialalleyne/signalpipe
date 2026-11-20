"""The Content writer agent: turns a brief from the SEO page into a full article draft, researched on the web."""
import time

from anthropic import beta_async_tool

from . import agentkit
from . import brain as brain_mod
from . import config, db

SYSTEM = """You write one article for a founder's website, from the brief below, for the buyers described in the
company profile. It should be the best page on the web for its target phrase: specific, practical, honest about
trade-offs, and written in the company's tone.

- Research first with web search: what the top results already say, current facts and numbers. Cite sources
  inline as markdown links. Never invent a statistic, a customer or a quote.
- Facts about the founder's own product come only from the company profile. Where one is missing, write a
  bracketed placeholder such as [YOUR PRICE].
- Use the target phrase in the title and naturally in the first paragraph. Clear H2 sections following the
  brief's outline, short paragraphs, 900 to 1,400 words, and a short FAQ at the end.
- No em dashes, no exclamation marks, no filler such as "in today's fast-paced world".
Finish by calling save_article once."""


async def write(brief: dict) -> dict:
    out: dict = {}

    @beta_async_tool
    async def save_article(title: str, meta_description: str, markdown: str) -> str:
        """Save the finished article. Call this once, at the end.

        Args:
            title: The page title, including the target phrase.
            meta_description: 120 to 160 characters for search results.
            markdown: The full article in markdown, starting with the first paragraph (not the title).
        """
        out.update(title=title, meta_description=meta_description, markdown=markdown)
        return "Saved."

    prompt = ("# Company\n" + brain_mod.summary(brain_mod.get()) + "\n\n# Brief\n"
              f"Title idea: {brief.get('title')}\nTarget phrase: {brief.get('target_keyword')}\n"
              f"Kind of page: {brief.get('intent')}\nWhy now: {brief.get('why')}\n"
              "Outline:\n" + "\n".join(f"- {o}" for o in brief.get("outline", [])) +
              (f"\nLink to: {brief['internal_link_to']}" if brief.get("internal_link_to") else ""))
    result = await agentkit.run("content", system=SYSTEM, subject=brief.get("title") or "article",
                                messages=[{"role": "user", "content": prompt + "\n\nResearch, then write and save the article."}],
                                tools=[save_article], web=agentkit.web_tools(config.LLM_MODEL, 5, 3), max_turns=10)
    if not out:
        return {"error": result.error or "The agent finished without saving an article."}
    drafts = db.get_setting("content_drafts", {}) or {}
    drafts[brief.get("title") or out["title"]] = {**out, "at": time.time()}
    db.set_setting("content_drafts", dict(list(drafts.items())[-30:]))
    agentkit.note(result.run_id, f"Wrote \"{out['title']}\" ({len(out['markdown'].split())} words), {result.searches} searches.")
    return out
