# main.py
import random
import time
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import quote
import json
import re
import httpx
import cloudscraper
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional, Dict, Any

# --- Config ---
BASE_URL = "https://www.animeunity.so"
CACHE_TTL = 300
MAX_RETRIES = 5
RETRY_DELAY = 1.5

# user agents to randomize fingerprint
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

# --- Globals (initialized in lifespan) ---
scraper: Optional[cloudscraper.CloudScraper] = None
httpx_client: Optional[httpx.AsyncClient] = None
scraper_lock = asyncio.Lock()
last_referer: str = BASE_URL
stream_cache: Dict[int, Dict[str, Any]] = {}


# --- Utilities for creating and warming scraper ---


def make_scraper() -> cloudscraper.CloudScraper:
    """Create a cloudscraper instance with randomized UA & headers."""
    ua = random.choice(USER_AGENTS)
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=5,  # small delay to mimic human-ish behavior (cloudscraper option)
    )
    # update default headers to look like a real browser
    s.headers.update(
        {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
        }
    )
    return s


async def ensure_scraper_ready(s: cloudscraper.CloudScraper) -> None:
    """
    Warm up the scraper by mimicking browser behavior:
      1) HEAD request to base URL (browser does this)
      2) small delay
      3) GET request to base URL to let Cloudflare JS run and set cookies (cf_clearance)
    Will raise on repeated failure.
    """
    loop = asyncio.get_running_loop()

    def _warmup() -> bool:
        try:
            # HEAD probe (like a browser preflight)
            head_resp = s.head(
                BASE_URL,
                headers={
                    "Accept": "*/*",
                    "Referer": BASE_URL,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=15,
            )
            # log for debugging
            print("🔎 HEAD warm-up:", getattr(head_resp, "status_code", "ERR"))

            # small human-like pause
            time.sleep(random.uniform(0.8, 1.6))

            # GET to trigger/complete challenge
            get_resp = s.get(BASE_URL, timeout=20)
            print("🌐 GET warm-up:", getattr(get_resp, "status_code", "ERR"))
            print("🔐 Cookies after warm-up:", s.cookies.get_dict())

            # success if GET returns 200 (cloudscraper may need to run challenge)
            return getattr(get_resp, "status_code", 0) == 200
        except Exception as e:
            print("❗ Warm-up exception:", e)
            return False

    ok = await loop.run_in_executor(None, _warmup)
    if not ok:
        raise RuntimeError("Cloudflare warm-up failed (HEAD/GET returned non-200)")


async def get_text_with_retries(url: str, referer: Optional[str] = None, retries: int = MAX_RETRIES) -> Dict[str, Any]:
    """
    Attempt to GET the URL using the global scraper.
    If we receive 403, regenerate scraper and retry (up to retries).
    Returns a dict: {status_code, text, url, headers, cookies}
    """
    global scraper
    loop = asyncio.get_running_loop()
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            # headers: include referer if provided
            headers = {"Referer": referer or last_referer}
            def _req():
                return scraper.get(url, headers=headers, timeout=20)
            resp = await loop.run_in_executor(None, _req)
            status = getattr(resp, "status_code", None)
            text = getattr(resp, "text", "")
            headers_ret = dict(getattr(resp, "headers", {}))
            cookies = scraper.cookies.get_dict()
            # If 403, regenerate and try again after warmup
            if status == 403:
                print(f"🛑 403 on attempt {attempt} for {url}. Regenerating scraper and retrying...")
                # regenerate scraper
                scraper = make_scraper()
                try:
                    await ensure_scraper_ready(scraper)
                except Exception as e:
                    print("❗ ensure_scraper_ready failed after regenerating scraper:", e)
                    # fallthrough to retry loop; maybe next attempt will succeed
                await asyncio.sleep(RETRY_DELAY)
                continue

            # success or other status: return basic details
            resolved_url = str(getattr(resp, "url", url))
            return {
                "status_code": status,
                "text": text,
                "url": resolved_url,
                "headers": headers_ret,
                "cookies": cookies,
            }
        except Exception as e:
            print(f"⚠️ get_text_with_retries attempt {attempt} error:", e)
            last_exc = e
            await asyncio.sleep(RETRY_DELAY)

    # After retries exhausted, raise last exception or return last known info
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts")


async def get_json_with_retries(url: str, referer: Optional[str] = None, retries: int = MAX_RETRIES) -> Dict[str, Any]:
    """
    Same as get_text_with_retries but returns parsed JSON when status==200
    """
    res = await get_text_with_retries(url, referer=referer, retries=retries)
    if res["status_code"] == 200:
        try:
            return {"status_code": 200, "json": json.loads(res["text"]), "url": res["url"], "headers": res["headers"], "cookies": res["cookies"]}
        except Exception:
            # fallback: sometimes endpoint returns JSON with proper headers but use .json via requests
            loop = asyncio.get_running_loop()
            def _req_json():
                r = scraper.get(url, headers={"Referer": referer or last_referer}, timeout=20)
                return r.status_code, r.json() if r.status_code == 200 else {}
            status, parsed = await loop.run_in_executor(None, _req_json)
            return {"status_code": status, "json": parsed, "url": url, "headers": {}, "cookies": scraper.cookies.get_dict()}
    else:
        return {"status_code": res["status_code"], "json": {}, "url": res["url"], "headers": res["headers"], "cookies": res["cookies"]}


# --- Parsers ---


def extract_json_from_html_with_thumbnails(html_content: str) -> list:
    """
    Attempt to find <archivio records="..."> and decode its records attribute JSON.
    """
    try:
        m = re.search(r'<archivio[^>]*records="([^"]+)"', html_content)
        if m:
            records_string = m.group(1)
        else:
            # fallback to bs4 if present
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_content, "html.parser")
                archivio = soup.find("archivio")
                if not archivio:
                    return []
                records_string = archivio.get("records") or ""
            except Exception:
                return []
        cleaned = records_string.replace(r'\="" \=""', r'\/').replace('=""', r'\"')
        return json.loads(cleaned)
    except Exception as e:
        print("❌ extract_json_from_html_with_thumbnails parse error:", e)
        return []


def extract_video_url_from_embed_html(html_content: str) -> Optional[str]:
    m = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    if m:
        return m.group(1)
    # fallback - any mp4/m3u8 link
    m2 = re.search(r"(https?://[^\s'\"<>]+(?:mp4|m3u8)[^\s'\"<>]*)", html_content)
    if m2:
        return m2.group(1)
    return None


# --- Lifespan / FastAPI app ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper, httpx_client, last_referer
    scraper = make_scraper()
    # Try warming the scraper (HEAD + GET); if it fails we'll still start but requests will attempt regeneration
    try:
        await ensure_scraper_ready(scraper)
        print("✅ Scraper warmed and ready")
    except Exception as e:
        print("⚠️ Scraper warmup failed at startup (will attempt on demand):", e)

    httpx_client = httpx.AsyncClient(timeout=None)
    yield
    try:
        if httpx_client:
            await httpx_client.aclose()
    except Exception:
        pass


app = FastAPI(title="AnimeUnity Proxy (cloudscraper + httpx streaming)", version="1.0.0", lifespan=lifespan)


# --- Endpoints using cloudscraper ---


@app.get("/search", summary="Search for anime by title (cloudscraper)")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    safe = quote(title)
    url = f"{BASE_URL}/archivio?title={safe}"

    # serialize handshake attempts
    async with scraper_lock:
        res = await get_text_with_retries(url, referer=last_referer, retries=MAX_RETRIES)

    print("➡️ /search status:", res["status_code"], "url:", url)
    print("🍪 cookies:", res.get("cookies"))

    if res["status_code"] != 200:
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    html = res["text"]
    records = extract_json_from_html_with_thumbnails(html)
    if not records:
        # helpful debugging snippet if parsing fails
        print("🔍 Got HTML snippet (first 1500 chars):", html[:1500])
        raise HTTPException(status_code=502, detail="Failed to parse archive records from upstream HTML")

    # update referer
    global last_referer
    last_referer = res.get("url", last_referer)

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


@app.get("/episodes", summary="Get episodes list (cloudscraper)")
async def get_episodes(anime_id: int):
    url_info = f"{BASE_URL}/info_api/{anime_id}/0"
    async with scraper_lock:
        res = await get_json_with_retries(url_info, referer=last_referer, retries=MAX_RETRIES)

    print("➡️ /episodes status:", res["status_code"], "url:", url_info)
    print("🍪 cookies:", res.get("cookies"))

    if res["status_code"] != 200:
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    info = res.get("json", {})
    count = info.get("episodes_count", 0)
    if count == 0:
        return {"anime_id": anime_id, "episodes": []}

    fetch_url = f"{url_info}?start_range=0&end_range={min(count, 120)}"
    async with scraper_lock:
        res2 = await get_json_with_retries(fetch_url, referer=last_referer, retries=MAX_RETRIES)

    if res2["status_code"] != 200:
        raise HTTPException(status_code=res2["status_code"], detail=f"Upstream returned {res2['status_code']}")

    # update referer
    global last_referer
    last_referer = res2.get("url", last_referer)

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


@app.get("/stream", summary="Resolve direct video URL (cloudscraper + cache)")
async def get_stream_url(episode_id: int):
    global last_referer
    now = time.time()
    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}

    embed_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    async with scraper_lock:
        res = await get_text_with_retries(embed_endpoint, referer=last_referer, retries=MAX_RETRIES)

    print("➡️ /stream resolve status:", res["status_code"], "url:", embed_endpoint)
    print("🍪 cookies:", res.get("cookies"))

    if res["status_code"] not in (200, 301, 302):
        raise HTTPException(status_code=res["status_code"], detail=f"Upstream returned {res['status_code']}")

    embed_target = res["headers"].get("location") or res["text"].strip()
    if not embed_target or not embed_target.startswith("http"):
        raise HTTPException(status_code=502, detail="Invalid embed target URL from upstream")

    async with scraper_lock:
        page = await get_text_with_retries(embed_target, referer=embed_endpoint, retries=MAX_RETRIES)

    if page["status_code"] != 200:
        raise HTTPException(status_code=page["status_code"], detail=f"Failed to fetch embed page ({page['status_code']})")

    video_url = extract_video_url_from_embed_html(page["text"])
    if not video_url:
        raise HTTPException(status_code=404, detail="No video URL found in embed page")

    stream_cache[episode_id] = {"url": video_url, "timestamp": now}
    last_referer = page.get("url", last_referer)
    return {"episode_id": episode_id, "stream_url": video_url, "cached": False}


# --- Stream video bytes via httpx (async) ---


@app.get("/stream_video", summary="Stream video bytes (httpx async)")
async def stream_video(request: Request, episode_id: int):
    # Resolve stream_url via internal /stream (goes through cloudscraper)
    try:
        data = await get_stream_url(episode_id)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve stream URL: {e}")

    stream_url = data.get("stream_url")
    if not stream_url:
        raise HTTPException(status_code=404, detail="Stream URL not found")

    range_header = request.headers.get("range")
    headers = {"Range": range_header} if range_header else {}

    # forward cookies from scraper session to httpx in case host requires them
    cookies = scraper.cookies.get_dict() if scraper else {}

    # use a fresh async httpx client (or reuse httpx_client if you prefer)
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

            status_code = resp.status_code
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

            async def generator():
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    yield chunk

            return StreamingResponse(generator(), headers=headers_to_send, status_code=status_code)


# --- Debug endpoint ---


@app.get("/_debug_info", summary="Debug: returns referer & cookies & cache keys")
async def debug_info():
    cookies = scraper.cookies.get_dict() if scraper else {}
    return {"last_referer": last_referer, "cookies": cookies, "stream_cache_keys": list(stream_cache.keys())}
