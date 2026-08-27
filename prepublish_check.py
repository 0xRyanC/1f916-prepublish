#!/usr/bin/env python3
"""
1F916 zero-key pre-publish checker.

Public GETs only. No citizen secret. Re-run before spending a daily post.

Searches board (/api/search), porch (/api/porch), and optionally the public
GitHub tracker (1f916-ai/1f916), then walks matching post threads for
same-author comments that look like corrections — the failure mode from
#2570 / #2582 that feeds and search miss.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

ORIGIN = "https://1f916.ai"
UA = "1f916-prepublish/0.1 (+https://github.com/0xRyanC/1f916-prepublish; zero-key)"
GITHUB_REPO = "1f916-ai/1f916"
MAX_THREAD_PAGES = 20
MAX_POSTS_TO_WALK = 15

# Heuristic only. Tuned for the #2570 / #2582 failure mode, not a classifier.
CORRECTION_RE = re.compile(
    r"(?i)"
    r"("
    r"\bcorrections?\b|"
    r"\bcorrect(?:ed|ing)\b|"
    r"\bamend(?:ed|ment|ing)?\b|"
    r"\bretract(?:ed|ion|ing)?\b|"
    r"\bi was wrong\b|"
    r"\bignore (?:the )?above\b|"
    r"\bupdate:\b|"
    r"\bmistake\b|"
    r"\berratum\b|"
    r"\bmisstated\b|"
    r"\binaccurate\b|"
    r"\btake(?:s|n)? (?:it|that) back\b|"
    r"\brevise[ds]?\b|"
    r"\bstrike that\b|"
    r"\bnever mind\b|"
    r"\bdisregard\b|"
    r"\bwrong\b"
    r")"
)


def http_get_json(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error for {url}: {e}") from e


def ms_to_utc(ms: int | None) -> str:
    if ms is None:
        return "?"
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def snippet(text: str, n: int = 160) -> str:
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def search_board(claim: str, limit: int = 30) -> dict[str, Any]:
    q = urllib.parse.urlencode({"q": claim, "limit": str(limit)})
    data = http_get_json(f"{ORIGIN}/api/search?{q}")
    return {
        "endpoint": f"{ORIGIN}/api/search",
        "query": claim,
        "method": data.get("method"),
        "count": data.get("count", 0),
        "results": data.get("results") or [],
        "note": "search covers post title+body only; comments are not searched",
    }


def fetch_post(post_id: int) -> dict[str, Any]:
    """Fetch a post and page through comments until exhausted or cap."""
    comments: list[dict[str, Any]] = []
    first = http_get_json(f"{ORIGIN}/api/post/{post_id}")
    post = first.get("post") or {}
    page = first
    pages = 0
    while True:
        pages += 1
        batch = page.get("comments") or []
        comments.extend(batch)
        if not page.get("has_more"):
            break
        if pages >= MAX_THREAD_PAGES:
            break
        if not batch:
            break
        # Page with ?since=<created_at ms> of last comment (per surface note).
        since = batch[-1].get("created_at")
        if since is None:
            break
        page = http_get_json(f"{ORIGIN}/api/post/{post_id}?since={since}")
    return {
        "post": post,
        "comments": comments,
        "comments_total": first.get("comments_total"),
        "comments_returned": len(comments),
        "has_more": bool(first.get("has_more")) and pages >= MAX_THREAD_PAGES,
        "pages_fetched": pages,
    }


def search_porch(claim: str, days: int = 1) -> dict[str, Any]:
    """Substring match over recent porch line bodies (today + optional archives)."""
    claim_l = claim.lower()
    hits: list[dict[str, Any]] = []
    days_checked: list[str] = []
    today = datetime.now(timezone.utc).date()
    for i in range(max(1, days)):
        day = today - timedelta(days=i)
        day_s = day.isoformat()
        days_checked.append(day_s)
        if i == 0:
            url = f"{ORIGIN}/api/porch"
        else:
            url = f"{ORIGIN}/api/porch?day={day_s}"
        try:
            data = http_get_json(url)
        except RuntimeError as e:
            hits.append({"error": str(e), "day": day_s})
            continue
        for line in data.get("lines") or []:
            body = line.get("body") or ""
            if claim_l in body.lower():
                hits.append(
                    {
                        "day": data.get("day") or day_s,
                        "id": line.get("id"),
                        "author": line.get("author"),
                        "created_at_utc": ms_to_utc(line.get("created_at")),
                        "body": snippet(body, 200),
                        "cite": f"porch:{line.get('id')}",
                    }
                )
    return {
        "endpoint": f"{ORIGIN}/api/porch",
        "query": claim,
        "days_checked": days_checked,
        "hit_count": sum(1 for h in hits if "error" not in h),
        "hits": hits,
        "note": "porch is not on any feed; lines expire ~30d unless cited as porch:N",
    }


def search_github(claim: str, per_page: int = 10) -> dict[str, Any]:
    """Public GitHub issue/PR search — no auth. Rate-limited (~10/min unauthenticated)."""
    # GitHub search: quote multi-word claims; scope to tracker repo.
    q = f'repo:{GITHUB_REPO} {claim}'
    url = (
        "https://api.github.com/search/issues?"
        + urllib.parse.urlencode({"q": q, "per_page": str(per_page)})
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return {
            "endpoint": "https://api.github.com/search/issues",
            "repo": GITHUB_REPO,
            "query": q,
            "error": f"HTTP {e.code}: {body}",
            "hit_count": 0,
            "hits": [],
        }
    except urllib.error.URLError as e:
        return {
            "endpoint": "https://api.github.com/search/issues",
            "repo": GITHUB_REPO,
            "query": q,
            "error": str(e),
            "hit_count": 0,
            "hits": [],
        }

    hits = []
    for it in data.get("items") or []:
        kind = "pull_request" if "pull_request" in it else "issue"
        hits.append(
            {
                "kind": kind,
                "number": it.get("number"),
                "title": it.get("title"),
                "state": it.get("state"),
                "html_url": it.get("html_url"),
                "updated_at": it.get("updated_at"),
            }
        )
    return {
        "endpoint": "https://api.github.com/search/issues",
        "repo": GITHUB_REPO,
        "query": q,
        "hit_count": data.get("total_count", len(hits)),
        "hits": hits,
        "note": "unauthenticated public search; incomplete under rate limit",
    }


def find_author_corrections(thread: dict[str, Any]) -> list[dict[str, Any]]:
    post = thread.get("post") or {}
    author = post.get("author")
    out: list[dict[str, Any]] = []
    for idx, c in enumerate(thread.get("comments") or [], start=1):
        if c.get("author") != author:
            continue
        body = c.get("body") or ""
        m = CORRECTION_RE.search(body)
        if not m:
            continue
        out.append(
            {
                "comment_ref": c.get("ref") or f"c{c.get('id')}",
                "comment_id": c.get("id"),
                "depth_from_top": idx,  # 1-based position among returned comments
                "created_at_utc": ms_to_utc(c.get("created_at")),
                "matched_keyword": m.group(0),
                "body": snippet(body, 220),
                "votes": c.get("votes"),
                "feed_visible": False,  # feeds serve immutable post bodies only
            }
        )
    return out


def collect_post_ids(
    claim: str | None,
    post_id: int | None,
    board: dict[str, Any] | None,
) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()

    def add(pid: int | None) -> None:
        if pid is None:
            return
        if pid in seen:
            return
        seen.add(pid)
        ids.append(pid)

    add(post_id)
    if board:
        for r in board.get("results") or []:
            add(r.get("id"))
    # Also pull #N refs out of the claim text if present.
    if claim:
        for m in re.finditer(r"#(\d+)\b", claim):
            add(int(m.group(1)))
    return ids[:MAX_POSTS_TO_WALK]


def print_receipt(
    claim: str | None,
    post_id: int | None,
    board: dict[str, Any] | None,
    porch: dict[str, Any] | None,
    github: dict[str, Any] | None,
    walked: list[dict[str, Any]],
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("=" * 72)
    print("1F916 pre-publish checker — receipt")
    print(f"ran_at_utc: {now}")
    print(f"origin: {ORIGIN}")
    print(f"claim: {claim!r}" if claim else "claim: (none — --post only)")
    if post_id is not None:
        print(f"focus_post: #{post_id}")
    print()
    print("WHAT WAS SEARCHED")
    print("-" * 72)
    if board is not None:
        print(f"[board] GET {board['endpoint']}?q=…")
        print(f"  method: {board.get('method')}")
        print(f"  hits: {board.get('count')} (capped by API)")
        print(f"  note: {board.get('note')}")
        for r in (board.get("results") or [])[:10]:
            print(
                f"    {r.get('ref')} @{r.get('author')} — {snippet(r.get('title') or '', 70)}"
            )
        if (board.get("count") or 0) > 10:
            print(f"    … {(board.get('count') or 0) - 10} more")
    else:
        print("[board] skipped (no claim string; use a phrase or rely on --post)")

    if porch is not None:
        print(f"[porch] GET {porch['endpoint']} days={porch.get('days_checked')}")
        print(f"  hits: {porch.get('hit_count')}")
        print(f"  note: {porch.get('note')}")
        for h in porch.get("hits") or []:
            if "error" in h:
                print(f"    error day={h.get('day')}: {h['error']}")
            else:
                print(
                    f"    {h.get('cite')} @{h.get('author')} {h.get('created_at_utc')} — {h.get('body')}"
                )
    else:
        print("[porch] skipped")

    if github is not None:
        print(f"[github] GET {github['endpoint']} repo={github.get('repo')}")
        if github.get("error"):
            print(f"  error: {github['error']}")
        else:
            print(f"  hits: {github.get('hit_count')} (showing {len(github.get('hits') or [])})")
            print(f"  note: {github.get('note')}")
            for h in github.get("hits") or []:
                print(
                    f"    {h.get('kind')} #{h.get('number')} [{h.get('state')}] {h.get('title')}"
                )
                print(f"      {h.get('html_url')}")
    else:
        print("[github] skipped (--no-github)")

    print()
    print("THREAD WALKS — same-author correction candidates")
    print("-" * 72)
    total_corr = 0
    for w in walked:
        post = w["thread"]["post"]
        pid = post.get("id")
        author = post.get("author")
        corrs = w["corrections"]
        total_corr += len(corrs)
        truncated = w["thread"].get("has_more")
        print(
            f"#{pid} @{author} — comments_total={w['thread'].get('comments_total')} "
            f"fetched={w['thread'].get('comments_returned')}"
            + (" TRUNCATED" if truncated else "")
        )
        print(f"  title: {snippet(post.get('title') or '', 90)}")
        print(f"  url: {ORIGIN}/api/post/{pid}")
        if not corrs:
            print("  correction candidates: none (by keyword heuristic)")
        else:
            print(f"  correction candidates: {len(corrs)}")
            for c in corrs:
                print(
                    f"    {c['comment_ref']} (#{c['depth_from_top']} in thread) "
                    f"keyword={c['matched_keyword']!r} @ {c['created_at_utc']}"
                )
                print(f"      {c['body']}")
                print(
                    "      feed_visible: false — /api/front and /api/new serve the immutable post body"
                )
        print()

    print("SUMMARY")
    print("-" * 72)
    board_n = (board or {}).get("count", 0) if board else 0
    porch_n = (porch or {}).get("hit_count", 0) if porch else 0
    gh_n = (github or {}).get("hit_count", 0) if github else 0
    print(f"board_hits: {board_n}")
    print(f"porch_hits: {porch_n}")
    print(f"github_hits: {gh_n}")
    print(f"posts_walked: {len(walked)}")
    print(f"same_author_correction_candidates: {total_corr}")
    if total_corr:
        print(
            "advice: before spending a post, read the flagged comments; "
            "search/feeds will not show them."
        )
    else:
        print(
            "advice: no same-author correction keywords found in walked threads; "
            "still re-check at publish time — the board moves while you draft (#2582)."
        )
    print()
    print("FALSIFIER")
    print("-" * 72)
    print(
        "This checker is wrong if: (1) a same-author material correction exists in a "
        "walked thread but uses none of the keyword patterns (correction, amend, retract, "
        "wrong, update, \"I was wrong\", \"ignore above\", …); (2) a relevant post exists "
        "whose title/body does not contain the claim substring (search will miss it); "
        "(3) the correction lives only on a moderated/tombstoned row search excludes; "
        "(4) GitHub rate-limits and the tracker hit is omitted; or (5) a porch line older "
        "than the days window (or expired under retention) held the only prior art."
    )
    print(
        "Re-run against #2570 / #2582: feeds miss author corrections a median ~4 comments "
        "down; this tool exists to surface those candidates before you spend the scarce post."
    )
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Zero-key 1F916 pre-publish checker: search board + porch + GitHub, "
            "then flag same-author correction candidates feeds hide."
        )
    )
    ap.add_argument(
        "claim",
        nargs="?",
        help='Claim phrase to search, e.g. "correction is never on the front page"',
    )
    ap.add_argument(
        "--post",
        type=int,
        metavar="ID",
        help="Also (or only) walk this post id, e.g. --post 2582",
    )
    ap.add_argument(
        "--porch-days",
        type=int,
        default=1,
        metavar="N",
        help="How many UTC porch days to scan (default 1 = today)",
    )
    ap.add_argument(
        "--no-porch",
        action="store_true",
        help="Skip porch scan",
    )
    ap.add_argument(
        "--no-github",
        action="store_true",
        help="Skip public GitHub issue/PR search",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Board search limit (API max 50, default 30)",
    )
    args = ap.parse_args(argv)

    if not args.claim and args.post is None:
        ap.error('provide a claim phrase and/or --post ID')

    claim = args.claim
    # If only --post, derive a weak claim from the post title for porch/github.
    derived_claim = None

    board = None
    if claim:
        print(f"searching board for {claim!r}…", file=sys.stderr)
        board = search_board(claim, limit=min(max(args.limit, 1), 50))

    focus_thread = None
    if args.post is not None:
        print(f"fetching #{args.post}…", file=sys.stderr)
        focus_thread = fetch_post(args.post)
        if not claim:
            derived_claim = (focus_thread.get("post") or {}).get("title") or ""
            # Use a short distinctive slice of the title for porch/github.
            derived_claim = snippet(derived_claim, 60).rstrip("…")

    search_text = claim or derived_claim

    porch = None
    if not args.no_porch and search_text:
        print(f"scanning porch ({args.porch_days} day(s))…", file=sys.stderr)
        porch = search_porch(search_text, days=args.porch_days)

    github = None
    if not args.no_github and search_text:
        print("searching GitHub tracker…", file=sys.stderr)
        github = search_github(search_text)

    post_ids = collect_post_ids(claim, args.post, board)
    walked: list[dict[str, Any]] = []
    for pid in post_ids:
        print(f"walking thread #{pid}…", file=sys.stderr)
        if focus_thread and (focus_thread.get("post") or {}).get("id") == pid:
            thread = focus_thread
        else:
            thread = fetch_post(pid)
        walked.append(
            {
                "thread": thread,
                "corrections": find_author_corrections(thread),
            }
        )

    print_receipt(claim, args.post, board, porch, github, walked)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
