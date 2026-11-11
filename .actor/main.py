"""Apify entry point: search YouTube for each query and save one row per result."""

from __future__ import annotations

import asyncio

from apify import Actor

from ytscrape import SearchFilter, YouTube


def _search(yt: YouTube, query: str, kind: SearchFilter, limit: int) -> list[dict]:
    rows = []
    if limit <= 0:
        return rows
    for pos, v in enumerate(yt.search(query, filter=kind, max_results=limit), start=1):
        vid = getattr(v, "video_id", None)
        if not vid:
            continue
        is_short = kind is SearchFilter.SHORTS
        rows.append({
            "query": query,
            "type": "short" if is_short else "video",
            "position": pos,
            "videoId": vid,
            "url": f"https://www.youtube.com/{'shorts/' + vid if is_short else 'watch?v=' + vid}",
            "title": v.title,
            "channel": v.channel,
            "channelId": v.channel_id,
            "views": v.views,
            "viewsText": v.views_text,
            "published": v.published_text,
            "publishedAt": v.published_at.isoformat() if v.published_at else None,
            "duration": v.duration,
        })
    return rows


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        queries = inp.get("searchQueries") or []
        with YouTube(language=inp.get("language", "en"), region=inp.get("region", "US"), min_interval=0.5) as yt:
            channels: dict[str, dict] = {}
            for q in queries:
                rows: list[dict] = []
                for kind, key in ((SearchFilter.VIDEOS, "maxVideos"), (SearchFilter.SHORTS, "maxShorts")):
                    try:
                        rows += _search(yt, q, kind, int(inp.get(key, 10)))
                    except Exception as e:  # keep going on one failed query
                        Actor.log.warning(f'"{q}" {kind.value} failed: {e}')
                if inp.get("fetchChannels", True):
                    for r in rows:
                        cid = r.get("channelId")
                        if cid and cid not in channels:
                            try:
                                c = yt.channel(cid)
                                channels[cid] = {"subscribers": c.subscribers, "channelVideoCount": c.video_count, "channelCountry": c.country}
                            except Exception as e:
                                Actor.log.warning(f"channel {cid} failed: {e}")
                                channels[cid] = {}
                        r.update(channels.get(cid, {}))
                await Actor.push_data(rows)
                Actor.log.info(f'"{q}": {len(rows)} rows')


if __name__ == "__main__":
    asyncio.run(main())
