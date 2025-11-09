from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from urllib.parse import quote
import asyncio
import json
import re
import time
import cloudscraper
import httpx
from typing import Optional, Dict, Any

# --- Configuration ---
BASE_URL = "https://www.animeunity.so"
CACHE_TTL = 300  # seconds for stream URL cache
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds

# App-global objects (initialized in lifespan)
scraper: Optional[cloudscraper.CloudScraper] = None
last_referer: str = BASE_URL
stream_cache: Dict[int, Dict[str, Any]] = {}
httpx_client: Optional[httpx.AsyncClient] = None
scraper_lock = asyncio.Lock()


# --- Lifespan (startup/shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper, httpx_client, last_referer

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "android", "mobile": True}
    )
    httpx_client = httpx.AsyncClient(timeout=None)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, lambda: scraper.get(BASE_URL, timeout=15))
        print("🔀 Lifespan initial fetch:", resp.status_code, "->", BASE_URL)
        last_referer = str(resp.url) if hasattr(resp, "url") else BASE_URL
        print("🔐 Initial cookies fetched:", scraper.cookies.get_dict())
    except Exception as e:
        print("❗ Lifespan initial fetch error:", e)

    yield

    try:
        if httpx_client:
            await httpx_client.aclose()
    except Exception:
        pass


app = FastAPI(
    title="AnimeUnity Proxy (cloudscraper + httpx streaming)",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Utilities ---

def extract_json_from_html_with_thumbnails(html_content: str) -> list:
    try:
        m = re.search(r'<archivio[^>]*records="([^"]+)"', html_content)
        if not m:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            archivio = soup.find("archivio")
            if not archivio:
                return []
            records_string = archivio.get("records") or ""
        else:
            records_string = m.group(1)
        cleaned = records_string.replace(r'\="" \=""', r'\/').replace('=""', r'\"')
        return json.loads(cleaned)
    except Exception as exc:
        print("❌ Error parsing archive JSON:", exc)
        return []


def extract_video_url_from_embed_html(html_content: str) -> Optional[str]:
    m = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    if m:
        return m.group(1)
    m2 = re.search(r"(https?://[^\s'\"<>]+(?:mp4|m3u8)[^\s'\"<>]*)", html_content)
    if m2:
        return m2.group(1)
    return None


async def run_scraper_get_text(url: str, headers: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    global scraper
    loop = asyncio.get_running_loop()

    def _call():
        resp = scraper.get(url, headers=headers or {}, timeout=timeout)
        return {
            "status_code": resp.status_code,
            "text": resp.text,
            "url": str(resp.url) if hasattr(resp, "url") else url,
            "headers": dict(resp.headers),
            "cookies": scraper.cookies.get_dict(),
        }

    return await loop.run_in_executor(None, _call)


async def run_scraper_get_json(url: str, headers: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    global scraper
    loop = asyncio.get_running_loop()

    def _call():
        resp = scraper.get(url, headers=headers or {}, timeout=timeout)
        return {
            "status_code": resp.status_code,
            "json": resp.json() if resp.status_code == 200 else {},
            "url": str(resp.url) if hasattr(resp, "url") else url,
            "headers": dict(resp.headers),
            "cookies": scraper.cookies.get_dict(),
        }

    return await loop.run_in_executor(None, _call)


async def simple_retry_scraper_text(url: str, referer: Optional[str] = None, retries: int = 3) -> Dict[str, Any]:
    global last_referer, scraper_lock
    headers = {
        "User-Agent": scraper.headers.get("User-Agent") if scraper and hasattr(scraper, "headers") else None,
    }
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, retries + 1):
        result = await run_scraper_get_text(url, headers=headers)
        status = result["status_code"]
        if status == 200:
            last_referer = result.get("url", last_referer)
            return result
        print(f"⚠️ fetch attempt {attempt} -> {url} returned {status}")
        await asyncio.sleep(RETRY_DELAY)
    return result


async def simple_retry_scraper_json(url: str, referer: Optional[str] = None, retries: int = 3) -> Dict[str, Any]:
    global last_referer
    headers = {}
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, retries + 1):
        result = await run_scraper_get_json(url, headers=headers)
        status = result["status_code"]
        if status == 200:
            last_referer = result.get("url", last_referer)
            return result
        print(f"⚠️ fetch-json attempt {attempt} -> {url} returned {status}")
        await asyncio.sleep(RETRY_DELAY)
    return result


# --- Endpoints ---

@app.get("/search")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    safe = quote(title)
    url = f"{BASE_URL}/archivio?title={safe}"

    async with scraper_lock:
        res = await simple_retry_scraper_text(url, referer=last_referer, retries=MAX_RETRIES)

    if res["status_code"] != 200:
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    html = res["text"]
    records = extract_json_from_html_with_thumbnails(html)
    if not records:
        raise HTTPException(status_code=502, detail="Failed to parse archive records")

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
            "genres": [g.get("name") for g in r.get("genres", [])] if r.get("genres") else [],
            "thumbnail": r.get("imageurl"),
        }
        for r in records
    ]


@app.get("/episodes")
async def get_episodes(anime_id: int):
    url_info = f"{BASE_URL}/info_api/{anime_id}/0"
    async with scraper_lock:
        res = await simple_retry_scraper_json(url_info, referer=last_referer, retries=MAX_RETRIES)

    if res["status_code"] != 200:
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    info = res.get("json", {})
    count = info.get("episodes_count", 0)
    if count == 0:
        return {"anime_id": anime_id, "episodes": []}

    fetch_url = f"{url_info}?start_range=0&end_range={min(count, 120)}"
    async with scraper_lock:
        res2 = await simple_retry_scraper_json(fetch_url, referer=last_referer, retries=MAX_RETRIES)

    if res2["status_code"] != 200:
        raise HTTPException(status_code=res2["status_code"], detail=f"Upstream returned {res2['status_code']}")

    episodes_data = res2.get("json", {})
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


@app.get("/stream")
async def get_stream_url(episode_id: int):
    global last_referer
    now = time.time()
    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}

    embed_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    async with scraper_lock:
        res = await simple_retry_scraper_text(embed_endpoint, referer=last_referer, retries=MAX_RETRIES)

    if res["status_code"] not in (200, 302, 301):
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    embed_target = res["headers"].get("location") or res["text"].strip()
    if not embed_target.startswith("http"):
        raise HTTPException(status_code=502, detail="Invalid embed target URL")

    async with scraper_lock:
        page = await simple_retry_scraper_text(embed_target, referer=embed_endpoint, retries=MAX_RETRIES)

    if page["status_code"] != 200:
        raise HTTPException(status_code=page["status_code"], detail="Failed to fetch embed page")

    video_url = extract_video_url_from_embed_html(page["text"])
    if not video_url:
        raise HTTPException(status_code=404, detail="No video URL found")

    stream_cache[episode_id] = {"url": video_url, "timestamp": now}
    last_referer = page.get("url", last_referer)
    return {"episode_id": episode_id, "stream_url": video_url, "cached": False}


@app.get("/stream_video")
async def stream_video(request: Request, episode_id: int):
    data = await get_stream_url(episode_id)
    stream_url = data.get("stream_url")
    if not stream_url:
        raise HTTPException(status_code=404, detail="Stream URL not found")

    range_header = request.headers.get("range")
    headers = {"Range": range_header} if range_header else {}
    cookies = scraper.cookies.get_dict() if scraper else {}

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", stream_url, headers=headers, cookies=cookies) as resp:
            if resp.status_code not in (200, 206):
                raise HTTPException(status_code=resp.status_code, detail=f"Upstream returned {resp.status_code}")

            content_length = resp.headers.get("content-length")
            content_type = resp.headers.get("content-type", "video/mp4")
            headers_to_send = {
                "Content-Type": content_type,
                "Accept-Ranges": resp.headers.get("Accept-Ranges", "bytes"),
            }

            if range_header and content_length:
                try:
                    cl = int(content_length)
                    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                    start = int(m.group(1)) if m else 0
                    end = int(m.group(2)) if m and m.group(2) else cl - 1
                    headers_to_send["Content-Range"] = f"bytes {start}-{end}/{cl}"
                    headers_to_send["Content-Length"] = str(end - start + 1)
                    status_code = 206
                except Exception:
                    headers_to_send["Content-Length"] = content_length
                    status_code = resp.status_code
            else:
                if content_length:
                    headers_to_send["Content-Length"] = content_length
                status_code = resp.status_code

            async def generator():
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    yield chunk

            return StreamingResponse(generator(), headers=headers_to_send, status_code=status_code)


@app.get("/_debug_info")
async def debug_info():
    cookies = scraper.cookies.get_dict() if scraper else {}
    return {"last_referer": last_referer, "cookies": cookies, "stream_cache_keys": list(stream_cache.keys())}