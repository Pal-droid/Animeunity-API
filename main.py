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
from typing import Optional

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
    """
    Initialize a shared httpx.AsyncClient with more realistic browser-like headers
    to reduce the chance of getting blocked (403). The client will keep cookies
    and automatically forward them between requests.
    """
    global shared_client, base_referer

    # Primary (desktop) user agent and extended "browser-like" headers
    initial_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/117.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Accept-Encoding is handled by httpx automatically; include it to mimic browser
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        # Some servers check the presence of these Sec-Fetch headers
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        # Typical CH / UA client hints (static strings, not real browser-sourced)
        'Sec-CH-UA': '"Chromium";v="117", "Not;A=Brand";v="8"',
        'Sec-CH-UA-Mobile': "?0",
        "Referer": BASE_URL,
        "Origin": BASE_URL,
    }

    # Create client with cookies preserved and redirects allowed by default
    shared_client = httpx.AsyncClient(headers=initial_headers, timeout=None, follow_redirects=True)

    # Attempt an initial GET to collect cookies and any set-cookie headers,
    # but treat 403 specially (we'll log and proceed; requests may still succeed later).
    try:
        resp = await shared_client.get(BASE_URL)
        # Log status and cookies for debug
        if resp.status_code == 403:
            print(f"❌ Initial fetch returned 403 for {BASE_URL}")
        else:
            resp.raise_for_status()
        base_referer = str(resp.url)
        print("✅ Shared HTTP client initialized")
        print("🔐 Initial cookies fetched:", shared_client.cookies.jar)  # jar shows Cookie objects
        # If server sent Set-Cookie headers, log them (helpful for debugging tokens)
        set_cookie = resp.headers.get("set-cookie")
        if set_cookie:
            print("🧾 Set-Cookie header on initial response:", set_cookie)
    except Exception as e:
        # Don't fail the whole app on initial 403; we still want endpoints to attempt requests.
        print(f"❌ Error fetching base URL: {e}")

    yield  # control handed to FastAPI

    # Shutdown
    if shared_client:
        await shared_client.aclose()
        print("🧹 Shared HTTP client closed")


# --- FastAPI App ---
app = FastAPI(
    title="AnimeUnity API Proxy (mimic-enhanced)",
    description="Async API to interact with AnimeUnity using httpx and shared sessions. "
                "This variant attempts to mimic browser headers to reduce 403s.",
    version="2.3.0",
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


def extract_video_url_from_embed_html(html_content: str) -> Optional[str]:
    match = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    return match.group(1) if match else None


async def retry_request(fn, *args, **kwargs):
    """Generic retry wrapper for transient network errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await fn(*args, **kwargs)
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"⚠️ Retry {attempt}/{MAX_RETRIES} after error: {e}")
            await asyncio.sleep(RETRY_DELAY)


def make_headers_with_referer(referer: Optional[str] = None, origin: Optional[str] = None, prefer_mobile: bool = False) -> dict:
    """
    Build request headers that look like a modern browser navigation.
    We rely on httpx's cookie jar to send cookies automatically (so we
    do NOT set Cookie header manually).
    """
    # Prefer the last-known referer, or caller-provided referer
    global base_referer
    referer_val = referer or base_referer or BASE_URL
    origin_val = origin or BASE_URL

    if prefer_mobile:
        user_agent = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Mobile Safari/537.36"
        sec_ch_ua_mobile = "?1"
        sec_fetch_site = "same-site"
    else:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
        sec_ch_ua_mobile = "?0"
        sec_fetch_site = "same-site"

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": referer_val,
        "Origin": origin_val,
        "Sec-Fetch-Site": sec_fetch_site,
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Sec-CH-UA": '"Chromium";v="117", "Not;A=Brand";v="8"',
        "Sec-CH-UA-Mobile": sec_ch_ua_mobile,
    }
    return headers


async def perform_get(url: str, referer: Optional[str] = None, origin: Optional[str] = None) -> httpx.Response:
    """
    Perform a GET with browser-like headers. If a 403 is received, try a second
    attempt with a mobile UA and slightly different headers.
    """
    global shared_client
    if not shared_client:
        raise RuntimeError("shared_client not initialized")

    headers = make_headers_with_referer(referer=referer, origin=origin, prefer_mobile=False)
    try:
        resp = await retry_request(shared_client.get, url, headers=headers)
        # If the server actively returns 403, attempt a slightly different header set
        if resp.status_code == 403:
            print(f"⚠️ Received 403 for {url} with desktop UA; retrying with mobile UA and explicit origin...")
            headers2 = make_headers_with_referer(referer=referer, origin=origin or BASE_URL, prefer_mobile=True)
            resp2 = await retry_request(shared_client.get, url, headers=headers2)
            return resp2
        return resp
    except httpx.HTTPStatusError:
        # propagate HTTP errors up to caller for further handling
        raise
    except Exception:
        # Re-raise for the caller to handle (retry_request will already have retried)
        raise


# --- API Endpoints ---

@app.get("/search", summary="Search for anime by title query (includes thumbnails)")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Title query must be provided.")
    global shared_client, base_referer

    safe_title = quote(title)
    search_url = f"{BASE_URL}/archivio?title={safe_title}"

    try:
        response = await perform_get(search_url, referer=base_referer, origin=BASE_URL)
        response.raise_for_status()
        # update referer for next requests
        base_referer = str(response.url)

        # Log cookies and Set-Cookie header to verify tokens/cookies are collected
        print("🍪 Cookies after search:", shared_client.cookies.jar)
        maybe_set_cookie = response.headers.get("set-cookie")
        if maybe_set_cookie:
            print("🧾 Set-Cookie received on search:", maybe_set_cookie)

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

    except httpx.HTTPStatusError as e:
        # Return the remote status code so the client can see reasons like 403
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/episodes", summary="Get episodes for a specific anime ID")
async def get_episodes(anime_id: int):
    global shared_client, base_referer
    info_url = f"{BASE_URL}/info_api/{anime_id}/0"

    try:
        info_response = await perform_get(info_url, referer=base_referer, origin=BASE_URL)
        info_response.raise_for_status()
        base_referer = str(info_response.url)

        # Log cookies for verification
        print("🍪 Cookies after info_api:", shared_client.cookies.jar)
        maybe_set_cookie = info_response.headers.get("set-cookie")
        if maybe_set_cookie:
            print("🧾 Set-Cookie received on info_api:", maybe_set_cookie)

        info_data = info_response.json()
        count = info_data.get("episodes_count", 0)
        if count == 0:
            return {"anime_id": anime_id, "episodes": []}

        fetch_url = f"{info_url}?start_range=0&end_range={min(count, 120)}"
        episodes_response = await perform_get(fetch_url, referer=base_referer, origin=BASE_URL)
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
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream", summary="Get direct video URL (cached & retried)")
async def get_stream_url(episode_id: int):
    global shared_client, base_referer
    now = time.time()

    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}

    embed_url_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    try:
        embed_response = await perform_get(embed_url_endpoint, referer=base_referer, origin=BASE_URL)
        # If this endpoint redirects with 302 and returns a Location header, we should respect it.
        # perform_get follows redirects by default unless server replies 302 with no Location (handled below).
        if embed_response.status_code in (301, 302) and "location" in embed_response.headers:
            embed_target_url = embed_response.headers.get("location")
        else:
            # Some endpoints return an actual URL in the body
            embed_target_url = embed_response.text.strip()

        print("🍪 Cookies while resolving embed:", shared_client.cookies.jar)
        maybe_set_cookie = embed_response.headers.get("set-cookie")
        if maybe_set_cookie:
            print("🧾 Set-Cookie received on embed resolve:", maybe_set_cookie)

        if not embed_target_url or not embed_target_url.startswith("http"):
            raise ValueError("Invalid embed URL")

        # Fetch the embed page (the referer is the embed endpoint)
        video_page_response = await perform_get(embed_target_url, referer=embed_url_endpoint, origin=BASE_URL)
        video_page_response.raise_for_status()

        video_url = extract_video_url_from_embed_html(video_page_response.text)
        if not video_url:
            raise HTTPException(status_code=404, detail="No video URL found")

        stream_cache[episode_id] = {"url": video_url, "timestamp": now}
        base_referer = str(video_page_response.url)
        return {"episode_id": episode_id, "stream_url": video_url, "cached": False}

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream_video", summary="Stream video file (with retries)")
async def stream_video(request: Request, episode_id: int):
    global shared_client
    if not shared_client:
        raise HTTPException(status_code=500, detail="Shared HTTP client not initialized")

    try:
        # Local call to /stream to resolve the actual video URL (we call our own endpoint)
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
        # When streaming the video we pass the Range header and use the shared client (which keeps cookies)
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
