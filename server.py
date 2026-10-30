"""Stack Exchange MCP server (stdio transport).

Never print() in this file: with stdio, stdout carries the MCP protocol itself.
Log to stderr instead.
"""
import logging
import os
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from client import StackExchangeClient
from models import (
    ErrorSearchResult,
    QuestionDetail,
    QuotaStatus,
    SearchResult,
    TagOverview,
    TagSearchResult,
)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO level, and our URLs contain the API key.
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_SITE = os.environ.get("SE_DEFAULT_SITE", "stackoverflow")

mcp = FastMCP(
    "stackexchange",
    instructions=(
        "Search Stack Overflow and other Stack Exchange sites for questions and answers. "
        "Use search_by_error_message when the user pastes an error, otherwise "
        "search_questions; then get_question to read a question with its best answers. "
        "Use search_tags to find exact tag names and get_tag_overview to learn about a "
        "technology. Always cite the returned links. Content is licensed CC BY-SA, so "
        "keep the author and link when quoting."
    ),
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

_client: StackExchangeClient | None = None


def get_client() -> StackExchangeClient:
    """Create the API client on first use (reads configuration from environment variables)."""
    global _client
    if _client is None:
        _client = StackExchangeClient(
            api_key=os.environ.get("STACKEXCHANGE_KEY"),
            cache_ttl=float(os.environ.get("SE_CACHE_TTL_SECONDS", "300")),
            max_body_chars=int(os.environ.get("SE_MAX_BODY_CHARS", "2000")),
        )
        mode = "authenticated" if _client.authenticated else "anonymous (low quota)"
        logging.getLogger(__name__).info("Stack Exchange client ready, %s", mode)
    return _client


@mcp.tool(annotations=READ_ONLY)
async def search_questions(
    query: Annotated[
        str,
        Field(description="Keywords to search for, e.g. 'asyncio sleep vs time.sleep'.", min_length=1, max_length=200),
    ],
    tags: Annotated[
        list[str] | None,
        Field(description="Optional tags to restrict results, e.g. ['python', 'asyncio'] (max 5).", max_length=5),
    ] = None,
    site: Annotated[
        str, Field(description="Stack Exchange site slug, e.g. 'stackoverflow' or 'serverfault'.")
    ] = DEFAULT_SITE,
    sort: Annotated[
        Literal["relevance", "votes", "activity", "creation"],
        Field(description="Result order. 'votes' favors well-regarded questions."),
    ] = "relevance",
    accepted_only: Annotated[
        bool, Field(description="Only return questions that have an accepted answer.")
    ] = False,
    limit: Annotated[int, Field(description="Number of results to return.", ge=1, le=10)] = 5,
) -> SearchResult:
    """Search Stack Overflow (or another Stack Exchange site) for questions about a topic.

    Use this to find existing discussions and solutions. It returns short summaries
    (title, score, tags, link), not full text. To read a question and its answers,
    pass a question_id from the results to get_question.
    """
    return await get_client().search_questions(query, tags, site, sort, accepted_only, limit)


@mcp.tool(annotations=READ_ONLY)
async def get_question(
    question_id: Annotated[int, Field(description="Question ID, from search_questions results.", gt=0)],
    site: Annotated[
        str, Field(description="Site slug the question belongs to, e.g. 'stackoverflow'.")
    ] = DEFAULT_SITE,
    max_answers: Annotated[
        int,
        Field(description="How many answers to include (accepted answer first, then by votes). Use 0 for the question only.", ge=0, le=5),
    ] = 3,
) -> QuestionDetail:
    """Read one question in full, together with its best answers.

    The question and answer bodies are returned as Markdown with code blocks preserved.
    Long posts are truncated and end with a link to the full text. Each item includes
    its author, link and license, so cite them when you use the content.
    """
    return await get_client().get_question(question_id, site, max_answers)


@mcp.tool(annotations=READ_ONLY)
async def get_quota_status(
    site: Annotated[str, Field(description="Any valid site slug.")] = DEFAULT_SITE,
) -> QuotaStatus:
    """Show the remaining Stack Exchange API quota and any active rate-limit wait.

    Call this if requests start failing, or before a large batch of searches.
    """
    return await get_client().quota_status(site)


@mcp.tool(annotations=READ_ONLY)
async def search_by_error_message(
    error_message: Annotated[
        str,
        Field(description="The error text or stack trace exactly as the user pasted it.", min_length=1, max_length=5000),
    ],
    tags: Annotated[
        list[str] | None,
        Field(description="Optional language/framework tags to improve precision, e.g. ['python'] (max 5).", max_length=5),
    ] = None,
    site: Annotated[
        str, Field(description="Stack Exchange site slug, e.g. 'stackoverflow'.")
    ] = DEFAULT_SITE,
    limit: Annotated[int, Field(description="Number of results to return.", ge=1, le=10)] = 5,
) -> ErrorSearchResult:
    """Find Stack Overflow discussions that match an error message or stack trace.

    Use this when the user pastes an error. The tool picks the most informative line
    and strips file paths, line numbers and memory addresses before searching; the
    query it actually used is returned as cleaned_query. Adding a language tag helps.
    Use get_question on a promising result to read the answers.
    """
    return await get_client().search_by_error_message(error_message, tags, site, limit)


@mcp.tool(annotations=READ_ONLY)
async def search_tags(
    query: Annotated[
        str, Field(description="Part of a tag name, e.g. 'asyn' or 'react'.", min_length=1, max_length=50)
    ],
    site: Annotated[
        str, Field(description="Stack Exchange site slug, e.g. 'stackoverflow'.")
    ] = DEFAULT_SITE,
    limit: Annotated[int, Field(description="Number of tags to return.", ge=1, le=20)] = 10,
) -> TagSearchResult:
    """Find valid tag names containing some text, most popular first.

    Use this before filtering other searches by tag, so you use tags that really exist
    instead of guessing (for example 'python-asyncio', not 'asyncio-python').
    """
    return await get_client().search_tags(query, site, limit)


@mcp.tool(annotations=READ_ONLY)
async def get_tag_overview(
    tag: Annotated[
        str,
        Field(description="Exact tag name, e.g. 'python-asyncio' or 'c#'. Use search_tags if unsure.", pattern=r"^[^\s;]+$", max_length=50),
    ],
    site: Annotated[
        str, Field(description="Stack Exchange site slug, e.g. 'stackoverflow'.")
    ] = DEFAULT_SITE,
    top_questions: Annotated[
        int, Field(description="How many of the most-voted questions to include (0 for none).", ge=0, le=5)
    ] = 3,
) -> TagOverview:
    """Get a short description of a tag (a technology or topic) and its most-voted questions.

    Use this to get oriented on an unfamiliar technology: it returns the tag's summary,
    how many questions use it, and the classic high-vote questions with links.
    """
    return await get_client().get_tag_overview(tag, site, top_questions)


# --- resources: read-only reference data an app can attach ---------------------------------
SEARCH_TIPS = """# Searching Stack Exchange effectively

- Use specific keywords from the problem (library, function, error wording), not whole sentences.
- Add a language or framework tag (e.g. python, react) to cut noise. Find exact tag names with search_tags.
- When the user pastes an error, use search_by_error_message instead of search_questions.
- sort='votes' surfaces canonical, well-regarded questions; sort='activity' surfaces recent discussion.
- accepted_only=true limits results to questions that have an accepted answer.
- Check dates (created / last_activity): old answers may be outdated for current library versions.
- If a search returns nothing, remove tags, use fewer or broader keywords, or try a synonym.
- Read the best answers with get_question, and always cite the link and author.
- Content is licensed CC BY-SA 3.0 or 4.0; keep attribution when quoting.
"""


@mcp.resource("stackexchange://sites", mime_type="text/markdown")
async def sites_resource() -> str:
    """List of Stack Exchange sites and the slug to pass as the `site` parameter."""
    sites = await get_client().list_sites()
    lines = ["# Stack Exchange sites (slug: name)", ""]
    for s in sites:
        suffix = f" - {s.audience}" if s.audience else ""
        lines.append(f"- {s.slug}: {s.name} ({s.url}){suffix}")
    return "\n".join(lines)


@mcp.resource("stackexchange://help/search-tips", mime_type="text/markdown")
def search_tips_resource() -> str:
    """Guidance on writing effective searches and using tags."""
    return SEARCH_TIPS


@mcp.resource("stackexchange://quota", mime_type="application/json")
async def quota_resource() -> str:
    """Live snapshot of the remaining API quota."""
    return (await get_client().quota_status()).model_dump_json()


# --- prompts: reusable workflows the user can start from -----------------------------------
@mcp.prompt()
def debug_error(error_message: str, language: str = "") -> str:
    """Debug an error using Stack Overflow: search, read the best threads, explain the fix."""
    lang = f" in {language}" if language else ""
    tag_hint = f" Use the tag '{language.lower()}' if it is a valid tag (check with search_tags)." if language else ""
    return (
        f"I'm hitting this error{lang}:\n\n{error_message}\n\n"
        "Please help me debug it using Stack Overflow:\n"
        f"1. Call search_by_error_message with the error.{tag_hint}\n"
        "2. Read the two most relevant results with get_question (max_answers=2).\n"
        "3. Explain the most likely cause and the fix, and show a short code example if useful.\n"
        "4. Note the age of the answers and anything that may be outdated for current versions.\n"
        "5. Cite the links of the threads you used."
    )


@mcp.prompt()
def compare_approaches(topic: str, option_a: str, option_b: str) -> str:
    """Compare two approaches using highly-voted Stack Overflow discussions."""
    return (
        f"Compare {option_a} and {option_b} for {topic}, based on Stack Overflow.\n\n"
        f"1. Search for '{option_a} vs {option_b}' and for each option on its own (search_questions, sort='votes').\n"
        "2. Read the best one or two threads with get_question.\n"
        "3. Summarize the trade-offs in a short table (when to use each, pros, cons).\n"
        "4. Give a recommendation for typical cases and mention caveats or outdated advice.\n"
        "5. Cite the links you used."
    )


@mcp.prompt()
def tag_primer(tag: str) -> str:
    """Build a beginner-friendly overview of a technology from its Stack Overflow tag."""
    return (
        f"I'm new to '{tag}'. Use Stack Overflow to build me a short primer:\n"
        f"1. Call get_tag_overview for '{tag}' (if the tag isn't found, use search_tags to find the right name).\n"
        "2. Read the top two questions with get_question.\n"
        "3. Write a beginner-friendly overview: what it is, the most common questions people ask, and typical pitfalls.\n"
        "4. Suggest what to learn next, and cite the links you used."
    )


if __name__ == "__main__":
    mcp.run()  # stdio transport
