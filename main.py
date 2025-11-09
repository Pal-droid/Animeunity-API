from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from urllib.parse import quote
import asyncio
import json
import re
import time
import cloudscraper
from bs4 import BeautifulSoup
from typing import Optional

# --- Configuration ---
BASE_URL = "https://www.animeunity.so"
CACHE_TTL = 300

stream_cache = {}

# --- Initialize cloudscraper ---
scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "android", "mobile": True}
)

# --- FastAPI App ---
app = FastAPI(
    title="AnimeUnity API Proxy (Cloudscraper)",
    description="FastAPI proxy using cloudscraper to bypass Cloudflare",
    version="3.0.0"
)

# --- Helper Functions ---

def extract_json_from_html_with_thumbnails(html_content: str) -> list:
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        archivio_tag = soup.find("archivio")
        if not archivio_tag:
            print("⚠️ No <archivio> tag found in HTML.")
            return []
        records_string = archivio_tag.get("records")
        if not records_string:
            print("⚠️ <archivio> tag found but missing 'records' attribute.")
            return []
        cleaned = records_string.replace(r'\="" \=""', r'\/').replace('=""', r'\"')
        return json.loads(cleaned)
    except Exception as e:
        print(f"❌ Error parsing anime records: {e}")
        return []

def extract_video_url_from_embed_html(html_content: str) -> Optional[str]:
    match = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    return match.group(1) if match else None

async def fetch_url(url: str) -> str:
    """Run cloudscraper in a thread to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: scraper.get(url).text)

async def fetch_json(url: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: scraper.get(url).json())

# --- API Endpoints ---

@app.get("/search", summary="Search for anime by title query (includes thumbnails)")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Title query must be provided.")
    safe_title = quote(title)
    search_url = f"{BASE_URL}/archivio?title={safe_title}"

    try:
        html = await fetch_url(search_url)
        anime_records = extract_json_from_html_with_thumbnails(html)
        return [
            {
                "id": r.get("id"),
                "title_en": r.get("title_eng", r.get("title")),
                "title_it": r.get("title_it", r.get("title")),
                "type": r.get("type"),
                "status": r.get("status"),
                "date": r.get("date"),
                "episodes_count": r.get("episodes_count"),
                "score": r.get("score"),
                "studio": r.get("studio"),
                "slug": r.get("slug"),
                "plot": (r.get("plot") or "").strip(),
                "genres": [g.get("name") for g in r.get("genres", [])],
                "thumbnail": r.get("imageurl"),
            }
            for r in anime_records
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search: {e}")

@app.get("/episodes", summary="Get episodes for a specific anime ID")
async def get_episodes(anime_id: int):
    info_url = f"{BASE_URL}/info_api/{anime_id}/0"
    try:
        info_data = await fetch_json(info_url)
        count = info_data.get("episodes_count", 0)
        if count == 0:
            return {"anime_id": anime_id, "episodes": []}
        fetch_url_episodes = f"{info_url}?start_range=0&end_range={min(count, 120)}"
        episodes_data = await fetch_json(fetch_url_episodes)
        return {
            "anime_id": anime_id,
            "episodes": [
                {
                    "episode_id": e.get("id"),
                    "number": e.get("number"),
                    "created_at": e.get("created_at"),
                    "visits": e.get("visite"),
                    "scws_id": e.get("scws_id"),
                }
                for e in episodes_data.get("episodes", [])
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get episodes: {e}")

@app.get("/stream", summary="Get direct video URL (cached & retried)")
async def get_stream_url(episode_id: int):
    now = time.time()
    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}

    embed_url_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    try:
        embed_html = await fetch_url(embed_url_endpoint)
        # Extract the actual embed URL
        video_url = extract_video_url_from_embed_html(embed_html)
        if not video_url:
            raise HTTPException(status_code=404, detail="No video URL found")
        stream_cache[episode_id] = {"url": video_url, "timestamp": now}
        return {"episode_id": episode_id, "stream_url": video_url, "cached": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stream URL: {e}")

@app.get("/stream_video", summary="Stream video file (with retries)")
async def stream_video(request: Request, episode_id: int):
    try:
        resp = await get_stream_url(episode_id)
        stream_url = resp.get("stream_url")
        if not stream_url:
            raise HTTPException(status_code=404, detail="Stream URL not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stream URL: {e}")

    range_header = request.headers.get("range")
    range_start, range_end = 0, None
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            range_start = int(match.group(1))
            range_end = int(match.group(2) or 0) or None

    async def video_generator():
        loop = asyncio.get_event_loop()
        def get_stream_content():
            headers = {"Range": f"bytes={range_start}-" if not range_end else f"bytes={range_start}-{range_end}"}
            r = scraper.get(stream_url, headers=headers, stream=True)
            for chunk in r.iter_content(1024 * 1024):
                yield chunk
        return await loop.run_in_executor(None, lambda: get_stream_content())

    # StreamingResponse can't take async generator from run_in_executor directly
    def sync_gen():
        headers = {"Range": f"bytes={range_start}-" if not range_end else f"bytes={range_start}-{range_end}"}
        r = scraper.get(stream_url, headers=headers, stream=True)
        for chunk in r.iter_content(1024 * 1024):
            yield chunk

    return StreamingResponse(sync_gen(), headers={"Content-Type": "video/mp4", "Accept-Ranges": "bytes"}, status_code=206 if range_header else 200)