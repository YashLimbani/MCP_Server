# DESIGN.md: Stack Exchange MCP Server

> Draft v0.1. Verify endpoint names, filters and quota numbers against the official Stack Exchange API docs before building.

## 1. Pitch

An MCP server that lets developers search Stack Overflow (and other Stack Exchange sites) from inside their AI assistant, so the assistant can ground debugging help in real, voted, accepted answers instead of guessing.

**Audience:** developers who use AI assistants (Claude, Cursor, VS Code Copilot, Gemini CLI) while coding.

## 2. Scope

**In scope (v0.1):** read-only search and retrieval of questions, answers and tags.
**Out of scope:** posting, voting, editing, OAuth write scopes, user profiles, chat.

Read-only keeps the server safe by default and the auth story simple.

## 3. Design principles

1. Few, well-described tools instead of one tool per endpoint.
2. Return **trimmed, readable** output (plain text/Markdown, not raw JSON dumps) to protect the model's context window.
3. Every error is an **actionable message** the model can reason about.
4. Never print to stdout (stdio transport). Log to stderr only.
5. Secrets come only from environment variables.

## 4. Configuration

| Env var | Required | Purpose |
|---|---|---|
| `STACKEXCHANGE_KEY` | No (recommended) | App key; raises the daily quota. Works without it at the lower anonymous quota. |
| `SE_DEFAULT_SITE` | No | Default site slug, `stackoverflow` if unset. |
| `SE_CACHE_TTL_SECONDS` | No | Response cache TTL, default 300. |
| `SE_MAX_BODY_CHARS` | No | Max characters of any post body returned, default 2000. |

## 5. Tools

All tools are read-only. Mark them with read-only/idempotent hints.

### 5.1 `search_questions`
Search questions by keywords and optional tags.
- **Use when:** the user wants existing discussions or solutions for a topic or problem.
- **Don't use when:** you already have a specific question ID (use `get_question`) or an exact error string (use `search_by_error_message`).

| Input | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | Free-text keywords. |
| `tags` | list[string] | `[]` | Up to 5; all must match. |
| `site` | string | default site | Site slug, e.g. `stackoverflow`, `serverfault`. |
| `sort` | enum `relevance`, `votes`, `activity`, `creation` | `relevance` | |
| `accepted_only` | bool | `false` | Only questions with an accepted answer. |
| `limit` | int 1-10 | 5 | |

**Returns:** list of `{question_id, title, score, answer_count, is_answered, tags, link, creation_date}`.

### 5.2 `get_question`
Fetch one question with its top answers.

| Input | Type | Default | Notes |
|---|---|---|---|
| `question_id` | int | required | |
| `site` | string | default site | |
| `max_answers` | int 0-5 | 3 | Accepted answer first, then by score. |

**Returns:** question title, trimmed body, tags, score, link, and the top answers (trimmed body, score, accepted flag, link).

### 5.3 `search_by_error_message`
Find discussions matching an error message or stack trace line.
- **Use when:** the user pastes an error. The tool cleans the input (strips file paths, line numbers, memory addresses, quotes) before searching.

| Input | Type | Default | Notes |
|---|---|---|---|
| `error_message` | string | required | Single most informative line. |
| `tags` | list[string] | `[]` | Language/framework tags improve precision. |
| `site` | string | default site | |
| `limit` | int 1-10 | 5 | |

**Returns:** same shape as `search_questions`, plus `cleaned_query` so the model can see what was searched.

### 5.4 `search_tags`
Find valid tag names (so the model doesn't guess tags that don't exist).

| Input | Type | Default |
|---|---|---|
| `query` | string | required |
| `site` | string | default site |
| `limit` | int 1-20 | 10 |

**Returns:** `{name, count, has_synonyms}` list.

### 5.5 `get_tag_overview`
Short description of a tag plus its most-voted questions.

| Input | Type | Default |
|---|---|---|
| `tag` | string | required |
| `site` | string | default site |
| `top_questions` | int 0-5 | 3 |

**Returns:** tag excerpt/wiki summary, question count, top questions.

### 5.6 `get_quota_status`
Report remaining API quota and whether a backoff is active, so the model can decide to slow down.

**Inputs:** none.
**Returns:** `{quota_remaining, quota_max, authenticated: bool, backoff_until: timestamp|null}`.

## 6. Resources

| URI | Contents |
|---|---|
| `stackexchange://sites` | List of supported site slugs with names (cached). |
| `stackexchange://help/search-tips` | Static guide on writing effective queries and tag usage. |
| `stackexchange://quota` | Live quota snapshot (same data as `get_quota_status`). |

## 7. Prompts

| Name | Arguments | Purpose |
|---|---|---|
| `debug_error` | `error_message`, `language` | Walks the assistant through: search by error, read top 2 questions, summarize likely cause and fix with links. |
| `compare_approaches` | `topic`, `option_a`, `option_b` | Searches both options, compares trade-offs using high-vote answers. |
| `tag_primer` | `tag` | Builds a beginner-friendly overview of a technology from tag overview and top questions. |

## 8. Error design

Errors return a clear message plus a suggested next step. Never leak stack traces or the API key.

| Case | Detection | Message to the model |
|---|---|---|
| Invalid input | pydantic validation | Names the bad field and allowed values. |
| Unknown site | site not in sites list | "Unknown site 'x'. Read `stackexchange://sites` for valid slugs." |
| Not found | empty result for an ID | "Question 123 not found on stackoverflow. It may be deleted or on another site." |
| No results | empty search | Return empty list plus hint: broaden query, remove tags, try `search_tags`. |
| Throttled / backoff | `backoff` field in response, or throttle error | "Rate limited. Retry after N seconds." Server also waits and retries once if N is small. |
| Quota exhausted | `quota_remaining == 0` | "Daily quota exhausted. Set STACKEXCHANGE_KEY for a higher quota, or retry after reset." |
| Bad key | API rejects key | "STACKEXCHANGE_KEY appears invalid. Check the key or unset it." |
| Upstream 5xx / timeout | HTTP error, timeout (10 s) | Retry up to 2 times with exponential backoff, then: "Stack Exchange is unavailable. Try again shortly." |

## 9. Rate limiting and caching

- Respect the API's `backoff` value: no request to the same method until the period passes.
- Track `quota_remaining` from every response and expose it via `get_quota_status`.
- Small client-side limiter (for example, max 5 requests/second) to avoid bursts.
- In-memory TTL cache keyed by (endpoint, params, site) to avoid repeated identical calls.
- Request compressed responses.

## 10. Output formatting

- Convert HTML bodies to Markdown or plain text; keep code blocks intact.
- Truncate bodies to `SE_MAX_BODY_CHARS`, ending with "[truncated, see link]".
- Always include the canonical `link` so the assistant can cite sources.
- Decode HTML entities in titles.

## 11. Testing plan

- **Unit tests:** mocked HTTP for each tool, each error row in section 8, cache and backoff behavior, error-message cleaning.
- **MCP Inspector:** call every tool, read every resource, run every prompt; check schemas and descriptions render well.
- **AI apps (minimum two):** Claude Desktop and Cursor (add VS Code, Claude Code or Gemini CLI if time allows). For each: config snippet, screenshot, quirks.
- **Selection tests:** 10 realistic user requests. Record which tool the model picked; fix descriptions where it picked wrong.
- **Clean-machine test:** a friend installs from the README only.

## 12. Packaging and release

- Python package with `uv`, entry point `stackexchange-mcp`, run via `uvx stackexchange-mcp`.
- stdio transport first; optional streamable HTTP later.
- MIT license, CHANGELOG, semantic versions starting at v0.1.0.
- Publish to PyPI, tag GitHub release, then submit to MCP directories.

## 13. Open questions

- Final package/server name (check PyPI and registries for conflicts).
- Whether to add `get_user_top_answers` in v0.2.
- Whether to support OAuth for private-team (Stack Overflow for Teams) use later.
