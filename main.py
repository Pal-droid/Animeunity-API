from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from urllib.parse import quote
import httpx
import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from bs4 import BeautifulSoup

# --- Configuration ---
BASE_URL = "https://www.animeunity.so"
CACHE_TTL = 300
MAX_RETRIES = 3
RETRY_DELAY = 2

stream_cache = {}
shared_client: httpx.AsyncClient | None = None
base_referer = BASE_URL

# --- Lifespan handler for startup/shutdown ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_client, base_referer

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    shared_client = httpx.AsyncClient(headers=headers, timeout=None, follow_redirects=True)

    # Initial request to get cookies and tokens
    try:
        initial_resp = await shared_client.get(BASE_URL)
        initial_resp.raise_for_status()
        base_referer = str(initial_resp.url)
        print("✅ Shared HTTP client initialized")
        print("🔐 Initial cookies fetched:", shared_client.cookies)
        print("📨 Initial headers:", shared_client.headers)
    except Exception as e:
        print(f"❌ Error fetching base URL: {e}")

    yield  # control handed to FastAPI

    # Shutdown
    if shared_client:
        await shared_client.aclose()
        print("🧹 Shared HTTP client closed")


# --- FastAPI App ---
app = FastAPI(
    title="AnimeUnity API Proxy",
    description="Async API to interact with AnimeUnity using httpx and shared sessions.",
    version="2.2.0",
    lifespan=lifespan
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


def extract_video_url_from_embed_html(html_content: str) -> str | None:
    match = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    return match.group(1) if match else None


async def retry_request(fn, *args, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await fn(*args, **kwargs)
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"⚠️ Retry {attempt}/{MAX_RETRIES} after error: {e}")
            await asyncio.sleep(RETRY_DELAY)


def make_headers_with_referer(referer: str | None = None) -> dict:
    global shared_client, base_referer
    headers = {
        "Referer": referer or base_referer,
        "User-Agent": shared_client.headers.get("User-Agent", ""),
        "Accept": shared_client.headers.get("Accept", ""),
        "Accept-Language": shared_client.headers.get("Accept-Language", ""),
    }
    return headers


# --- API Endpoints ---

@app.get("/search", summary="Search for anime by title query (includes thumbnails)")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Title query must be provided.")
    global shared_client, base_referer
    safe_title = quote(title)
    search_url = f"{BASE_URL}/archivio?title={safe_title}"
    headers = make_headers_with_referer(base_referer)
    response = await retry_request(shared_client.get, search_url, headers=headers)
    response.raise_for_status()
    base_referer = str(response.url)
    anime_records = extract_json_from_html_with_thumbnails(response.text)
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


@app.get("/episodes", summary="Get episodes for a specific anime ID")
async def get_episodes(anime_id: int):
    global shared_client, base_referer
    info_url = f"{BASE_URL}/info_api/{anime_id}/0"
    headers = make_headers_with_referer(base_referer)
    info_response = await retry_request(shared_client.get, info_url, headers=headers)
    info_response.raise_for_status()
    base_referer = str(info_response.url)
    info_data = info_response.json()
    count = info_data.get("episodes_count", 0)
    if count == 0:
        return {"anime_id": anime_id, "episodes": []}
    fetch_url = f"{info_url}?start_range=0&end_range={min(count, 120)}"
    episodes_response = await retry_request(shared_client.get, fetch_url, headers=headers)
    episodes_response.raise_for_status()
    base_referer = str(episodes_response.url)
    episodes_data = episodes_response.json()
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


@app.get("/stream", summary="Get direct video URL (cached & retried)")
async def get_stream_url(episode_id: int):
    global shared_client, base_referer
    now = time.time()
    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}
    embed_url_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    headers = make_headers_with_referer(base_referer)
    embed_response = await retry_request(
        shared_client.get, embed_url_endpoint, headers=headers, follow_redirects=False
    )
    embed_response.raise_for_status()
    embed_target_url = (
        embed_response.headers.get("Location")
        if embed_response.status_code == 302
        else embed_response.text.strip()
    )
    print("🍪 Current cookies:", shared_client.cookies)
    print("📨 Forwarding headers:", headers)
    if not embed_target_url.startswith("http"):
        raise ValueError("Invalid embed URL")
    video_page_response = await retry_request(
        shared_client.get, embed_target_url, headers=make_headers_with_referer(embed_url_endpoint)
    )
    video_page_response.raise_for_status()
    video_url = extract_video_url_from_embed_html(video_page_response.text)
    if not video_url:
        raise HTTPException(status_code=404, detail="No video URL found")
    stream_cache[episode_id] = {"url": video_url, "timestamp": now}
    base_referer = str(video_page_response.url)
    return {"episode_id": episode_id, "stream_url": video_url, "cached": False}


@app.get("/stream_video", summary="Stream video file (with retries)")
async def stream_video(request: Request, episode_id: int):
    global shared_client
    if not shared_client:
        raise HTTPException(status_code=500, detail="Shared HTTP client not initialized")
    try:
        resp = await retry_request(shared_client.get, f"http://127.0.0.1:8000/stream?episode_id={episode_id}")
        data = resp.json()
        stream_url = data.get("stream_url")
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
        headers = {"Range": f"bytes={range_start}-" if not range_end else f"bytes={range_start}-{range_end}"}
        async with shared_client.stream("GET", stream_url, headers=headers) as video_resp:
            if video_resp.status_code not in (200, 206):
                raise HTTPException(status_code=video_resp.status_code, detail="Failed to fetch video")
            async for chunk in video_resp.aiter_bytes(1024 * 1024):
                yield chunk
    head_resp = await retry_request(shared_client.head, stream_url)
    content_length = int(head_resp.headers.get("content-length", 0))
    headers_to_send = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
    }
    if range_header and content_length:
        end = content_length - 1 if not range_end else range_end
        headers_to_send["Content-Range"] = f"bytes {range_start}-{end}/{content_length}"
        headers_to_send["Content-Length"] = str(end - range_start + 1)
        status_code = 206
    elif content_length:
        headers_to_send["Content-Length"] = str(content_length)
        status_code = 200
    else:
        status_code = 200
    return StreamingResponse(video_generator(), headers=headers_to_send, status_code=status_code)
