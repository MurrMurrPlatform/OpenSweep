"""Shared limit/offset paging for collection routes.

Usage in routes:

    async def list_things(
        response: Response,
        page: Page = Depends(page_params),
        ...
    ):
        return paginate(items, page, response)

`limit` has NO default. The board views build their own tab counts, lane
totals and "N of M" labels from the full array they get back
(front_end/src/views/FindingsView.vue, TicketsView.vue, NewsView.vue,
QueueView.vue), so a default page size would not shrink a response — it
would make those numbers quietly wrong. Paging is therefore opt-in: a
caller that never sends `limit` keeps today's whole-result behaviour, and a
caller that does gets `X-Total-Count` back so it can tell how far it is
through the set. Turning on a default cap means reworking those views
first.

The response body stays a bare JSON array. The frontend hand-maintains its
DTO types and calls `apiGet<FindingDTO[]>` directly
(front_end/src/services/api.ts), so an envelope would be a breaking change
across every call site for no gain here.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query, Response

# Ceiling on an explicit ?limit=. Not a default — see the module docstring.
MAX_LIMIT = 1000

TOTAL_COUNT_HEADER = "X-Total-Count"


@dataclass(frozen=True)
class Page:
    """One requested window. `limit is None` means "no window, send it all"."""

    limit: int | None = None
    offset: int = 0


def page_params(
    limit: int | None = Query(
        None,
        ge=1,
        le=MAX_LIMIT,
        description="Page size. Omit for the whole result set.",
    ),
    offset: int = Query(0, ge=0, description="Items to skip before the page."),
) -> Page:
    return Page(limit=limit, offset=offset)


def paginate[T](items: list[T], page: Page, response: Response) -> list[T]:
    """Cut `page` out of `items`, reporting the full size in a header.

    X-Total-Count is always set, so a paging caller can see the total
    without a second request and never has to guess whether a short page
    means "the end" or "exactly one page left".
    """
    response.headers[TOTAL_COUNT_HEADER] = str(len(items))
    if page.limit is None:
        return items[page.offset :] if page.offset else items
    return items[page.offset : page.offset + page.limit]
