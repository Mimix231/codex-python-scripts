#!/usr/bin/env python3
"""
search_web_context.py

Search the public web for current documentation and return compact context
blocks that Abyss can feed back into the coding pipeline.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Sequence

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "GlitchAbyss/1.0 (+local-tool)"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search the public web and return compact context for coding tasks."
    )
    parser.add_argument("--query", required=True, help="Search query.")
    parser.add_argument(
        "--domain",
        action="append",
        default=[],
        help="Optional domain filter. May be repeated.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of results to return. Default: 5",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=12.0,
        help="HTTP timeout in seconds. Default: 12",
    )
    parser.add_argument(
        "--json-report",
        action="store_true",
        help="Emit a JSON summary after the human-readable output.",
    )
    return parser.parse_args(argv)


def build_query(query: str, domains: list[str]) -> str:
    filters = [f"site:{domain.strip()}" for domain in domains if domain.strip()]
    return " ".join([query.strip(), *filters]).strip()


def fetch_search_html(query: str, timeout: float) -> str:
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_URL,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def strip_tags(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    normalized = html.unescape(without_tags)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_result_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if "duckduckgo.com" not in parsed.netloc:
        return url
    query = urllib.parse.parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return urllib.parse.unquote(uddg[0])
    return url


def parse_results(document: str, limit: int) -> list[SearchResult]:
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>(?P<tail>[\s\S]{0,1600}?)</div>',
        re.IGNORECASE,
    )
    results: list[SearchResult] = []

    for match in pattern.finditer(document):
        url = normalize_result_url(html.unescape(match.group("url")))
        title = strip_tags(match.group("title"))
        tail = match.group("tail")
        snippet_match = re.search(
            r'result__snippet[^>]*>(?P<snippet>[\s\S]*?)<',
            tail,
            re.IGNORECASE,
        )
        snippet = strip_tags(snippet_match.group("snippet")) if snippet_match else ""
        if not title or not url:
            continue
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= limit:
            break

    return results


def emit_report(query: str, results: list[SearchResult], json_report: bool) -> int:
    if results:
        print(f"Search results for: {query}")
        for index, result in enumerate(results, start=1):
            print(f"{index}. {result.title}")
            print(f"   {result.url}")
            if result.snippet:
                print(f"   {result.snippet}")
    else:
        print(f"No results found for: {query}")

    if json_report:
        payload = {
            "query": query,
            "resultCount": len(results),
            "results": [asdict(result) for result in results],
            "source": "duckduckgo-html",
        }
        print(json.dumps(payload, indent=2))

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    query = build_query(args.query, args.domain)
    if not query:
        raise SystemExit("query must not be empty")

    try:
        document = fetch_search_html(query, args.timeout)
        results = parse_results(document, max(1, min(args.limit, 10)))
    except Exception as error:  # pragma: no cover - network failure path
        print(f"search failed: {error}", file=sys.stderr)
        return 1

    return emit_report(query, results, args.json_report)


if __name__ == "__main__":
    raise SystemExit(main())
