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
from httpx import StreamClosed
from typing import Optional, Dict, Any
import html
from bs4 import BeautifulSoup

# --- Configuration ---
BASE_URL = "https://corsproxy.io/?url=https://www.animeunity.so"
CACHE_TTL = 300  # seconds for stream URL cache
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds

# App-global objects (initialized in lifespan)
scraper: Optional[cloudscraper.CloudScraper] = None
httpx_client: Optional[httpx.AsyncClient] = None
scraper_lock = asyncio.Lock()
last_referer: str = BASE_URL
stream_cache: Dict[int, Dict[str, Any]] = {}


# --- Lifespan (startup/shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper, httpx_client, last_referer

    print("🚀 Initializing cloudscraper + httpx client")
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    httpx_client = httpx.AsyncClient(timeout=None)

    # Warm up scraper & cookies
    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, lambda: scraper.get(BASE_URL, timeout=15))
        print("🌐 Warmup status:", resp.status_code)
        if resp.status_code == 200:
            last_referer = str(resp.url)
        print("🔐 Initial cookies:", scraper.cookies.get_dict())
    except Exception as e:
        print("⚠️ Lifespan warmup error:", e)

    yield

    # Shutdown cleanup
    try:
        if httpx_client:
            await httpx_client.aclose()
    except Exception:
        pass
    scraper = None
    print("🛑 Shutdown complete.")


app = FastAPI(
    title="AnimeUnity Proxy (cloudscraper + httpx streaming)",
    version="1.1.0",
    lifespan=lifespan,
)


# --- Utilities ---

def extract_json_from_html_with_thumbnails(html_content: str) -> list:
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        archivio = soup.find("archivio")
        if not archivio:
            return []
        records_string = archivio.get("records") or ""
        records_string = html.unescape(records_string)
        return json.loads(records_string)
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


async def run_scraper_get(url: str, as_json: bool = False, referer: Optional[str] = None, timeout: int = 20) -> Dict[str, Any]:
    global scraper, last_referer

    def _call():
        headers = {
            "User-Agent": scraper.headers.get("User-Agent"),
            "Referer": referer or BASE_URL,
            "Origin": BASE_URL,
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        }
        resp = scraper.get(url, headers=headers, timeout=timeout)
        return {
            "status": resp.status_code,
            "text": resp.text,
            "json": resp.json() if as_json and resp.status_code == 200 else {},
            "url": str(resp.url),
            "headers": dict(resp.headers),
            "cookies": scraper.cookies.get_dict(),
        }

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _call)
    if result["status"] == 200:
        last_referer = result["url"]
    elif result["status"] == 403:
        print("⚠️ Got 403 — refreshing scraper session...")
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return result


async def retry_scraper(url: str, as_json: bool = False, referer: Optional[str] = None, retries: int = MAX_RETRIES):
    for attempt in range(1, retries + 1):
        res = await run_scraper_get(url, as_json=as_json, referer=referer)
        if res["status"] == 200:
            return res
        print(f"⚠️ Attempt {attempt}/{retries} for {url} failed with {res['status']}")
        await asyncio.sleep(RETRY_DELAY)
    return res


# --- Endpoints ---

@app.get("/search")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Missing title")
    safe = quote(title)
    url = f"{BASE_URL}/archivio?title={safe}"

    async with scraper_lock:
        res = await retry_scraper(url, referer=last_referer)

    if res["status"] != 200:
        raise HTTPException(status_code=res["status"], detail="Upstream error")

    html_content = res["text"]
    records = extract_json_from_html_with_thumbnails(html_content)
    if not records:
        raise HTTPException(status_code=502, detail="Failed to parse archive records")

    return [
        {
            "id": r.get("id"),
            "title_en": r.get("title_eng", r.get("title")),
            "title_it": r.get("title_it", r.get("title")),
            "type": r.get("type"),
            "status": r.get("status"),
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
    url = f"{BASE_URL}/info_api/{anime_id}/0"
    async with scraper_lock:
        res = await retry_scraper(url, as_json=True, referer=last_referer)

    if res["status"] != 200:
        raise HTTPException(status_code=res["status"], detail="Upstream error")

    info = res.get("json", {})
    count = info.get("episodes_count", 0)
    if count == 0:
        return {"anime_id": anime_id, "episodes": []}

    fetch_url = f"{url}?start_range=0&end_range={min(count, 120)}"
    async with scraper_lock:
        res2 = await retry_scraper(fetch_url, as_json=True, referer=last_referer)

    if res2["status"] != 200:
        raise HTTPException(status_code=res2["status"], detail="Upstream error")

    data = res2.get("json", {})
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
            for e in data.get("episodes", [])
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
        res = await retry_scraper(embed_endpoint, referer=last_referer)

    if res["status"] not in (200, 301, 302):
        raise HTTPException(status_code=res["status"], detail="Upstream error")

    embed_target = res["headers"].get("location") or res["text"].strip()
    if not embed_target.startswith("http"):
        raise HTTPException(status_code=502, detail="Invalid embed target")

    async with scraper_lock:
        page = await retry_scraper(embed_target, referer=embed_endpoint)

    if page["status"] != 200:
        raise HTTPException(status_code=page["status"], detail="Failed to fetch embed page")

    video_url = extract_video_url_from_embed_html(page["text"])
    if not video_url:
        raise HTTPException(status_code=404, detail="No video URL found")

    stream_cache[episode_id] = {"url": video_url, "timestamp": now}
    last_referer = page.get("url", last_referer)
    return {"episode_id": episode_id, "stream_url": video_url, "cached": False}

@app.get("/embed")
async def stream_video(request: Request, episode_id: int):
    data = await get_stream_url(episode_id)
    stream_url = data.get("stream_url")

    if not stream_url:
        raise HTTPException(status_code=404, detail="Stream URL not found")

    range_header = request.headers.get("range")
    cookies = scraper.cookies.get_dict() if scraper else {}

    # Build initial headers (may include Range)
    upstream_headers = {}
    if range_header:
        upstream_headers["Range"] = range_header

    try:
        # First attempt: request with the client's Range (if provided)
        async with httpx_client.stream(
            "GET", stream_url, headers=upstream_headers, cookies=cookies, timeout=None
        ) as upstream:

            status = upstream.status_code
            h = upstream.headers

            # If upstream returned 206 but DID NOT provide Content-Range,
            # treat upstream as broken and re-request WITHOUT Range to get a clean 200.
            if status == 206 and "content-range" not in h:
                # close this upstream context and re-request without Range
                print("⚠️ Upstream returned 206 without Content-Range — re-requesting without Range")
                # exiting the "async with" will close the connection; do it by finishing the block
                pass  # fallthrough to re-request below

            else:
                # Normal happy path: upstream provided either 200, or 206+Content-Range
                response_headers = {
                    "Content-Type": h.get("content-type", "video/mp4"),
                    "Accept-Ranges": "bytes",
                    "Access-Control-Allow-Origin": "*",
                    "Content-Disposition": "inline",
                }
                if status == 206 and "content-range" in h:
                    response_headers["Content-Range"] = h["content-range"]

                # Never forward Content-Length (avoid mismatches)
                response_headers.pop("content-length", None)

                async def chunk_generator():
                    try:
                        async for chunk in upstream.aiter_bytes(1024 * 1024):
                            yield chunk
                    except httpx.StreamClosed:
                        # Upstream closed early — stop streaming cleanly
                        print("⚠️ Upstream closed connection during streaming (stream closed).")
                        return
                    except Exception as exc:
                        # Any other error: log and stop silently to avoid TaskGroup crash
                        print("⚠️ Streaming error while iterating upstream:", exc)
                        return

                return StreamingResponse(
                    chunk_generator(),
                    status_code=status,
                    headers=response_headers,
                )

        # If we reach here it means we exited the first async with due to the broken 206 case.
        # Re-request without the Range header to get a full 200 response.
        async with httpx_client.stream(
            "GET", stream_url, headers={}, cookies=cookies, timeout=None
        ) as upstream2:
            status2 = upstream2.status_code
            h2 = upstream2.headers

            if status2 not in (200, 206):
                raise HTTPException(status_code=status2, detail="Upstream returned error on re-request")

            response_headers = {
                "Content-Type": h2.get("content-type", "video/mp4"),
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": "inline",
            }

            # If the re-request somehow gives a proper 206+Content-Range, forward it
            if status2 == 206 and "content-range" in h2:
                response_headers["Content-Range"] = h2["content-range"]

            response_headers.pop("content-length", None)

            async def chunk_generator2():
                try:
                    async for chunk in upstream2.aiter_bytes(1024 * 1024):
                        yield chunk
                except httpx.StreamClosed:
                    print("⚠️ Upstream closed connection during streaming (stream closed) on re-request.")
                    return
                except Exception as exc:
                    print("⚠️ Streaming error while iterating upstream on re-request:", exc)
                    return

            return StreamingResponse(
                chunk_generator2(),
                status_code=status2,
                headers=response_headers,
            )

    except httpx.RequestError as e:
        # network-level errors (DNS, connect, etc.)
        raise HTTPException(status_code=502, detail=f"Upstream request error: {e}")
    except Exception as e:
        # Fallback
        raise HTTPException(status_code=500, detail=f"Streaming error: {e}")