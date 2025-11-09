# main.py
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
scraper_lock = asyncio.Lock()  # ensure only one thread does certain scraper startup tasks at once


# --- Lifespan (startup/shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper, httpx_client, last_referer

    # Create a persistent cloudscraper Session (synchronous)
    # Choose a realistic mobile UA to match the site's expected UA (adjust if desired)
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "android", "mobile": True}
    )

    # Async httpx client used only for streaming video
    httpx_client = httpx.AsyncClient(timeout=None)

    # Try an initial request in a thread so cloudflare handshake may be established early.
    # If it returns a JS challenge page (403), cloudscraper will attempt to resolve it on subsequent calls.
    loop = asyncio.get_running_loop()
    try:
        # call scraper.get in executor to avoid blocking event loop
        resp = await loop.run_in_executor(None, lambda: scraper.get(BASE_URL, timeout=15))
        print("🔀 Lifespan initial fetch:", resp.status_code, "->", BASE_URL)
        # update referer from the resolved URL (in case of redirects)
        last_referer = str(resp.url) if hasattr(resp, "url") else BASE_URL
        # log current cookies
        try:
            print("🔐 Initial cookies fetched (dict):", scraper.cookies.get_dict())
        except Exception:
            print("🔐 Could not read scraper cookies (unexpected).")
    except Exception as e:
        print("❗ Lifespan initial fetch error (non-fatal):", e)

    yield

    # shutdown
    try:
        if httpx_client:
            await httpx_client.aclose()
    except Exception:
        pass


# Create FastAPI app with lifespan
app = FastAPI(
    title="AnimeUnity Proxy (cloudscraper + httpx streaming)",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Utilities / Parsers ---

def extract_json_from_html_with_thumbnails(html_content: str) -> list:
    """
    Parse the <archivio records="..."> custom tag that animeunity embeds.
    This mirrors your original function; it's permissive and returns [] on error.
    """
    try:
        # naive extraction: find <archivio ... records="(json)"> or similar
        # We'll search for archivio tag and records attribute
        import re
        m = re.search(r'<archivio[^>]*records="([^"]+)"', html_content)
        if not m:
            # fallback: try finding archivio and attribute extraction via parsing
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            archivio = soup.find("archivio")
            if not archivio:
                return []
            records_string = archivio.get("records") or ""
        else:
            records_string = m.group(1)

        # undo known escapes used on the site
        cleaned = records_string.replace(r'\="" \=""', r'\/').replace('=""', r'\"')
        # try to JSON decode
        return json.loads(cleaned)
    except Exception as exc:
        print("❌ Error parsing archive JSON:", exc)
        return []


def extract_video_url_from_embed_html(html_content: str) -> Optional[str]:
    """
    Extract a JS variable like: window.downloadUrl = 'https://...';
    """
    m = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    if m:
        return m.group(1)
    # alternative: maybe the embed returns a raw url in body
    m2 = re.search(r"(https?://[^\s'\"<>]+(?:mp4|m3u8)[^\s'\"<>]*)", html_content)
    if m2:
        return m2.group(1)
    return None


async def run_scraper_get_text(url: str, headers: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    """
    Run scraper.get(url) in executor and return a dict with status_code, text, url, headers, cookies.
    This ensures we don't block the event loop.
    """
    global scraper
    if not scraper:
        raise RuntimeError("scraper not initialized")

    loop = asyncio.get_running_loop()

    def _call():
        # cloudscraper uses requests.Session; call it synchronously here
        resp = scraper.get(url, headers=headers or {}, timeout=timeout)
        return {
            "status_code": resp.status_code,
            "text": resp.text,
            "url": str(resp.url) if hasattr(resp, "url") else url,
            "headers": dict(resp.headers),
            "cookies": scraper.cookies.get_dict(),
        }

    # run in default executor
    return await loop.run_in_executor(None, _call)


async def run_scraper_get_json(url: str, headers: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    """
    Run scraper.get(url).json() in executor. Return a dict wrapper with 'status_code' and 'json'.
    """
    global scraper
    if not scraper:
        raise RuntimeError("scraper not initialized")

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
    """
    Try to fetch text via cloudscraper with retries. Update last_referer on success.
    """
    global last_referer, scraper_lock
    headers = {
        "User-Agent": scraper.headers.get("User-Agent") if scraper and hasattr(scraper, "headers") else None,
    }
    # Set Referer if given
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, retries + 1):
        result = await run_scraper_get_text(url, headers=headers)
        status = result["status_code"]
        # If response looks like Cloudflare challenge (status 403 and JS page), cloudscraper may need another request.
        if status == 200:
            # update referer
            last_referer = result.get("url", last_referer)
            return result
        else:
            # log debugging snippet for the caller
            print(f"⚠️ fetch attempt {attempt} -> {url} returned {status}")
            # If challenge is present, allow cloudscraper another attempt after a short sleep
            await asyncio.sleep(RETRY_DELAY)
    return result  # return last result even if non-200


async def simple_retry_scraper_json(url: str, referer: Optional[str] = None, retries: int = 3) -> Dict[str, Any]:
    headers = {}
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, retries + 1):
        result = await run_scraper_get_json(url, headers=headers)
        status = result["status_code"]
        if status == 200:
            # update referer
            global last_referer
            last_referer = result.get("url", last_referer)
            return result
        else:
            print(f"⚠️ fetch-json attempt {attempt} -> {url} returned {status}")
            await asyncio.sleep(RETRY_DELAY)
    return result


# --- Endpoints using cloudscraper for site/API access ---


@app.get("/search", summary="Search for anime by title query (uses cloudscraper)")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    safe = quote(title)
    url = f"{BASE_URL}/archivio?title={safe}"

    # ensure only one initial handshake runs at a time to avoid racing the JS challenge
    async with scraper_lock:
        res = await simple_retry_scraper_text(url, referer=last_referer, retries=MAX_RETRIES)

    # debug info
    print("➡️ /search called", "status:", res["status_code"], "url:", url)
    print("🍪 Current scraper cookies:", res.get("cookies"))

    if res["status_code"] != 200:
        # return remote status to client; helpful to see 403/5xx reasons
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    html = res["text"]
    records = extract_json_from_html_with_thumbnails(html)
    if not records:
        # If no records found it's possible Cloudflare challenge page returned despite 200 (rare)
        # Return the HTML snippet for debugging (trim to safe length)
        snippet = html[:2000]
        print("🔍 Debug HTML snippet (first 2000 chars):\n", snippet)
        # Return 502 to indicate upstream parsing issue
        raise HTTPException(status_code=502, detail="Failed to parse archive records from upstream HTML")

    # map the records to expected response shape
    out = []
    for r in records:
        out.append(
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
        )

    return out


@app.get("/episodes", summary="Get episodes for a specific anime ID (uses cloudscraper)")
async def get_episodes(anime_id: int):
    url_info = f"{BASE_URL}/info_api/{anime_id}/0"
    # get info (json)
    async with scraper_lock:
        res = await simple_retry_scraper_json(url_info, referer=last_referer, retries=MAX_RETRIES)

    print("➡️ /episodes called", "status:", res["status_code"], "url:", url_info)
    print("🍪 Current scraper cookies:", res.get("cookies"))

    if res["status_code"] != 200:
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    info = res.get("json", {})
    count = info.get("episodes_count", 0)
    if count == 0:
        return {"anime_id": anime_id, "episodes": []}

    # fetch episodes list (bounded)
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


@app.get("/stream", summary="Get direct video URL (uses cloudscraper & caching)")
async def get_stream_url(episode_id: int):
    now = time.time()
    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}

    embed_endpoint = f"{BASE_URL}/embed-url/{episode_id}"

    async with scraper_lock:
        res = await simple_retry_scraper_text(embed_endpoint, referer=last_referer, retries=MAX_RETRIES)

    print("➡️ /stream called", "status:", res["status_code"], "url:", embed_endpoint)
    print("🍪 Current scraper cookies:", res.get("cookies"))

    if res["status_code"] not in (200, 302, 301):
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    # embed_target may be in Location header or in the response body
    embed_target = None
    if res["headers"].get("location"):
        embed_target = res["headers"]["location"]
    else:
        # sometimes the endpoint returns a URL in the body
        embed_target = res["text"].strip()

    if not embed_target or not embed_target.startswith("http"):
        raise HTTPException(status_code=502, detail="Invalid embed target URL from upstream")

    # fetch embed target page (may contain js var with download URL)
    async with scraper_lock:
        page = await simple_retry_scraper_text(embed_target, referer=embed_endpoint, retries=MAX_RETRIES)

    if page["status_code"] != 200:
        raise HTTPException(status_code=page["status_code"], detail=f"Failed to fetch embed page ({page['status_code']})")

    video_url = extract_video_url_from_embed_html(page["text"])
    if not video_url:
        raise HTTPException(status_code=404, detail="No video URL found in embed page")

    stream_cache[episode_id] = {"url": video_url, "timestamp": now}
    # update referer to last fetched page
    global last_referer
    last_referer = page.get("url", last_referer)

    return {"episode_id": episode_id, "stream_url": video_url, "cached": False}


# --- Streaming (httpx async) endpoint ---
@app.get("/stream_video", summary="Stream video bytes (async httpx) — uses httpx for streaming")
async def stream_video(request: Request, episode_id: int):
    # Resolve stream_url via internal /stream (so it runs through cloudscraper)
    try:
        data = await get_stream_url(episode_id)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve stream URL: {e}")

    stream_url = data.get("stream_url")
    if not stream_url:
        raise HTTPException(status_code=404, detail="Stream URL not found")

    # Prepare range header if provided by client
    range_header = request.headers.get("range")
    headers = {}
    if range_header:
        headers["Range"] = range_header

    # Pass any cookies the scraper session may have to httpx (in case video host needs them)
    cookies: Dict[str, str] = {}
    try:
        # scraper.cookies is requests' cookiejar
        cookies = scraper.cookies.get_dict() if scraper else {}
    except Exception:
        cookies = {}

    # Use an async httpx client (from lifespan)
    async with httpx.AsyncClient(timeout=None) as client:
        # stream the response from the remote server
        async with client.stream("GET", stream_url, headers=headers, cookies=cookies) as resp:
            # If remote server doesn't like our cookies/headers we may get 403/401 etc
            if resp.status_code not in (200, 206):
                # raise on non-success so caller sees the upstream status
                raise HTTPException(status_code=resp.status_code, detail=f"Upstream streaming returned {resp.status_code}")

            # grab content-length for response headers (if present)
            content_length = resp.headers.get("content-length")
            content_type = resp.headers.get("content-type", "video/mp4")

            # Build headers to send back to client
            headers_to_send = {
                "Content-Type": content_type,
                "Accept-Ranges": resp.headers.get("Accept-Ranges", "bytes"),
            }
            if range_header and content_length:
                try:
                    cl = int(content_length)
                    # parse requested start — Range header format "bytes=start-end"
                    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                    start = int(m.group(1)) if m else 0
                    end = int(m.group(2)) if m and m.group(2) else cl - 1
                    headers_to_send["Content-Range"] = f"bytes {start}-{end}/{cl}"
                    headers_to_send["Content-Length"] = str(end - start + 1)
                    status_code = 206
                except Exception:
                    headers_to_send["Content-Length"] = content_length
                    status_code = resp.status_code if resp.status_code in (200, 206) else 200
            else:
                if content_length:
                    headers_to_send["Content-Length"] = content_length
                status_code = resp.status_code if resp.status_code in (200, 206) else 200

            async def generator():
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    yield chunk

            return StreamingResponse(generator(), headers=headers_to_send, status_code=status_code)


# --- Health / Debug endpoint (optional) ---
@app.get("/_debug_info", summary="Debug: show current cookies & referer (not for production)")
async def debug_info():
    try:
        cookies = scraper.cookies.get_dict() if scraper else {}
    except Exception:
        cookies = {}
    return {"last_referer": last_referer, "cookies": cookies, "stream_cache_keys": list(stream_cache.keys())}
