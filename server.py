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
from models import QuestionDetail, QuotaStatus, SearchResult

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
        "Use search_questions to find discussions, then get_question to read a question "
        "with its best answers. Always cite the returned links. Content is licensed "
        "CC BY-SA, so keep the author and link when quoting."
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


if __name__ == "__main__":
    mcp.run()  # stdio transport
