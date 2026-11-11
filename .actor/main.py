"""Apify entry point for Glimsera topic research.

Two inputs, used together or alone:
- seeds: expanded through YouTube search suggestions (what people type now)
  into a list of real search phrases.
- searchQueries: phrases to score as given.

Every phrase is searched once per region. Output rows:
- type="topic": one per phrase and region, with demand and gap scores.
- type="video": the results behind each topic (when saveVideos is on).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import string
import time
from datetime import datetime, timezone

import requests
from apify import Actor

from ytscrape import SearchFilter, YouTube

SUGGEST_URL = "https://suggestqueries-clients6.youtube.com/complete/search"


def suggestions(session: requests.Session, q: str, hl: str, gl: str) -> list[str]:
    try:
        r = session.get(SUGGEST_URL, params={"client": "firefox", "ds": "yt", "hl": hl, "gl": gl, "q": q}, timeout=15)
        data = json.loads(r.text)
        return [s for s in data[1] if isinstance(s, str)]
    except Exception:
        return []


def discover(seeds: list[str], modifiers: list[str], alphabet: bool, hl: str, gl: str) -> dict[str, int]:
    """Return {phrase: best suggestion rank} from YouTube autocomplete."""
    session = requests.Session()
    found: dict[str, int] = {}
    for seed in seeds:
        prompts = [seed] + [f"{seed} {m}" for m in modifiers] + [f"{m} {seed}" for m in ("which", "is", "can you")]
        if alphabet:
            prompts += [f"{seed} {c}" for c in string.ascii_lowercase]
        for p in prompts:
            for rank, s in enumerate(suggestions(session, p, hl, gl), start=1):
                s = s.strip().lower()
                if s and (s not in found or rank < found[s]):
                    found[s] = rank
            time.sleep(0.15)
    return found


def days_old(v) -> float | None:
    if not v.published_at:
        return None
    return max((datetime.now(timezone.utc) - v.published_at).total_seconds() / 86400, 1.0)


def score_topic(videos: list[dict], recent_days: int, small_subs: int, old_days: int) -> dict:
    n = len(videos)
    if not n:
        return {"results": 0}
    views = [v["views"] or 0 for v in videos]
    ages = [v["ageDays"] for v in videos if v["ageDays"] is not None]
    recent = [v for v in videos if v["ageDays"] is not None and v["ageDays"] <= recent_days]
    recent_vpd = sorted((v["viewsPerDay"] for v in recent if v["viewsPerDay"] is not None), reverse=True)
    top5 = videos[:5]
    subs_known = [v for v in top5 if v.get("subscribers") is not None]
    out = {
        "results": n,
        "maxViews": max(views),
        "medianViews": int(statistics.median(views)),
        "medianAgeDays": int(statistics.median(ages)) if ages else None,
        "shareTop5Old": round(sum(1 for v in top5 if (v["ageDays"] or 0) >= old_days) / len(top5), 2),
        "shareTop5SmallChannel": round(sum(1 for v in subs_known if v["subscribers"] < small_subs) / len(subs_known), 2) if subs_known else None,
        "recentVideos": len(recent),
        "recentBestViewsPerDay": int(recent_vpd[0]) if recent_vpd else 0,
        "recentMedianViewsPerDay": int(statistics.median(recent_vpd)) if recent_vpd else 0,
        "smallChannelWin": any(
            (v.get("subscribers") or 10**9) < small_subs and (v["views"] or 0) >= 50_000 for v in videos
        ),
    }
    # Demand now: how fast fresh videos on this phrase pull views (log-ish buckets 0-5).
    best = out["recentBestViewsPerDay"]
    out["demandNow"] = next((s for s, t in ((5, 5000), (4, 1500), (3, 500), (2, 150), (1, 30)) if best >= t), 0)
    # Lifetime demand: proof the phrase has ever pulled views (0-5).
    mv = out["maxViews"]
    out["demandEver"] = next((s for s, t in ((5, 2_000_000), (4, 500_000), (3, 150_000), (2, 50_000), (1, 10_000)) if mv >= t), 0)
    # Gap: old top results, small channels ranking, few fresh uploads (0-5).
    gap = 2.5 * out["shareTop5Old"] + 1.5 * (out["shareTop5SmallChannel"] or 0) + (1.0 if out["recentVideos"] <= 2 else 0)
    out["gap"] = round(min(gap, 5), 1)
    out["score"] = round(0.45 * out["demandNow"] + 0.2 * out["demandEver"] + 0.35 * out["gap"], 2)
    return out


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        regions = inp.get("regions") or [inp.get("region", "US")]
        hl = inp.get("language", "en")
        per_query = int(inp.get("maxVideos", 10))
        save_videos = inp.get("saveVideos", True)
        fetch_channels = inp.get("fetchChannels", True)
        max_topics = int(inp.get("maxTopics", 150))
        recent_days = int(inp.get("recentDays", 120))
        small_subs = int(inp.get("smallChannelSubs", 50_000))
        old_days = int(inp.get("oldDays", 730))

        channel_cache: dict[str, int | None] = {}
        for gl in regions:
            phrases: dict[str, int] = {q.strip().lower(): 0 for q in inp.get("searchQueries") or [] if q.strip()}
            if inp.get("seeds"):
                found = discover(inp["seeds"], inp.get("modifiers") or ["vs", "for", "explained", "mistakes", "routine", "types"], inp.get("alphabet", False), hl, gl)
                Actor.log.info(f"[{gl}] autocomplete found {len(found)} phrases")
                for k, r in found.items():
                    phrases.setdefault(k, r)
            items = sorted(phrases.items(), key=lambda kv: kv[1])[:max_topics]

            with YouTube(language=hl, region=gl, min_interval=0.4) as yt:
                for q, rank in items:
                    videos: list[dict] = []
                    try:
                        for pos, v in enumerate(yt.search(q, filter=SearchFilter.VIDEOS, max_results=per_query), start=1):
                            if not getattr(v, "video_id", None):
                                continue
                            age = days_old(v)
                            videos.append({
                                "type": "video", "region": gl, "query": q, "position": pos,
                                "videoId": v.video_id, "url": f"https://www.youtube.com/watch?v={v.video_id}",
                                "title": v.title, "channel": v.channel, "channelId": v.channel_id,
                                "views": v.views, "published": v.published_text,
                                "ageDays": int(age) if age else None,
                                "viewsPerDay": int((v.views or 0) / age) if age else None,
                                "duration": v.duration,
                            })
                    except Exception as e:
                        Actor.log.warning(f'[{gl}] "{q}" search failed: {e}')
                    if fetch_channels:
                        for v in videos[:5]:
                            cid = v["channelId"]
                            if cid and cid not in channel_cache:
                                try:
                                    channel_cache[cid] = yt.channel(cid).subscribers
                                except Exception:
                                    channel_cache[cid] = None
                            v["subscribers"] = channel_cache.get(cid)
                    topic = {"type": "topic", "region": gl, "query": q, "autocompleteRank": rank or None,
                             **score_topic(videos, recent_days, small_subs, old_days)}
                    await Actor.push_data([topic] + (videos if save_videos else []))
            Actor.log.info(f"[{gl}] scored {len(items)} phrases")


if __name__ == "__main__":
    asyncio.run(main())
