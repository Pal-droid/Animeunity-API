# main.py
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
from contextlib import asynccontextmanager

# --- Configuration ---
BASE_URL = "https://www.animeunity.so"
CACHE_TTL = 300
USER_AGENT_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)
USER_AGENT_MOBILE = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
)

stream_cache = {}
scraper: Optional[cloudscraper.CloudScraper] = None
base_referer = BASE_URL

# --- Lifespan / startup handler to create cloudscraper instance ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper, base_referer
    # Create a cloudscraper instance configured to mimic a mobile Chrome browser
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "android", "mobile": True}
    )

    # Set default headers on scraper (helps some checks)
    scraper.headers.update({
        "User-Agent": USER_AGENT_MOBILE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        # Note: cloudscraper will handle other CF handshake bits
    })

    # Try an initial GET to collect cookies and observe server response
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: scraper.get(BASE_URL, timeout=20))
        if resp.status_code == 403:
            print(f"❌ Initial fetch returned 403 for {BASE_URL}")
        else:
            print(f"✅ Initial fetch returned {resp.status_code} for {BASE_URL}")
        base_referer = str(resp.url)
        # Log cookie names and values for debugging
        cookie_items = scraper.cookies.get_dict()
        print("🔐 Initial cookies fetched (dict):", cookie_items)
        # Check specifically for Cloudflare clearance token
        if "cf_clearance" in cookie_items:
            print("🟢 cf_clearance cookie present.")
        else:
            print("⚠️ cf_clearance cookie not present (Cloudflare JS challenge may remain).")
        # Log a tiny bit of the response for diagnosis
        text_snippet = (resp.text or "")[:1200]
        print("🔍 Initial HTML snippet (first 1200 chars):\n", text_snippet)
    except Exception as e:
        print(f"❌ Error during initial fetch: {e}")

    yield

    # Shutdown: nothing special to do for cloudscraper, but be explicit
    try:
        if scraper:
            scraper.close()
            print("🧹 cloudscraper closed")
    except Exception:
        pass


app = FastAPI(
    title="AnimeUnity Proxy (cloudscraper + debug)",
    description="Proxy that uses cloudscraper to bypass Cloudflare; verbose debug logging for <archivio> parsing.",
    version="4.0.0",
    lifespan=lifespan
)


# --- Helper functions with verbose logging ---

def debug_print_response(resp, note: str = ""):
    """Print useful debug info from a requests.Response returned by cloudscraper."""
    try:
        print(f"\n--- DEBUG RESPONSE {note} ---")
        print("URL:", getattr(resp, "url", None))
        print("Status:", getattr(resp, "status_code", None))
        print("Headers (first lines):")
        for k, v in list(getattr(resp, "headers", {}).items())[:6]:
            print(f"  {k}: {v}")
        print("Cookies (dict):", scraper.cookies.get_dict() if scraper else {})
        snippet = (resp.text or "")[:1200]
        print("HTML snippet (first 1200 chars):")
        print(snippet)
        print(f"--- END DEBUG RESPONSE {note} ---\n")
    except Exception as e:
        print("Error printing debug response:", e)


def extract_json_from_html_with_thumbnails(html_content: str) -> list:
    """Extract JSON-like data from the <archivio> tag and clean custom escapes. Verbose debug logs."""
    try:
        print("🔍 Debug: HTML snippet for parsing (first 1200 chars):")
        print(html_content[:1200].replace("\n", " "))

        soup = BeautifulSoup(html_content, "html.parser")
        archivio_tag = soup.find("archivio")

        if not archivio_tag:
            print("⚠️ No <archivio> tag found in HTML.")
            # Helpful debug outputs
            # 1) Look for obvious Cloudflare challenge markers:
            if "Checking your browser" in html_content or "cf_chl_jschl" in html_content or "captcha" in html_content.lower():
                print("⚠️ The returned page contains Cloudflare challenge markers (Checking your browser / captcha / cf_chl_jschl).")
            # 2) Print the top-level tags to inspect what was returned
            top_tags = [tag.name for tag in soup.find_all(limit=20)]
            print("🔍 Top-level tags in returned HTML (first 20):", top_tags)
            # 3) Print a larger snippet of the full soup for manual inspection
            print("🔍 Full HTML (first 5000 chars):")
            print(str(soup)[:5000].replace("\n", " "))
            return []

        records_string = archivio_tag.get("records")
        if not records_string:
            print("⚠️ <archivio> tag found but missing 'records' attribute.")
            print("🔍 Debug: archivio tag content (full):")
            print(str(archivio_tag))
            return []

        cleaned = records_string.replace(r'\="" \=""', r'\/').replace('=""', r'\"')
        try:
            data = json.loads(cleaned)
            print(f"✅ Parsed {len(data)} anime records successfully.")
            return data
        except json.JSONDecodeError as e:
            print(f"❌ JSON decode error: {e}")
            print("🔍 Debug: cleaned records string snippet (first 1000 chars):")
            print(cleaned[:1000])
            return []

    except Exception as e:
        print(f"❌ Unexpected error parsing anime records: {e}")
        return []


def extract_video_url_from_embed_html(html_content: str) -> Optional[str]:
    match = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
    return match.group(1) if match else None


# --- Synchronous scraper wrappers executed in thread pool ---

async def fetch_response(url: str, referer: Optional[str] = None, headers: Optional[dict] = None):
    """
    Use cloudscraper to GET the URL in a background thread. Returns the requests.Response object.
    """
    loop = asyncio.get_event_loop()

    def do_get():
        # Merge headers if provided (scraper has default headers)
        kw = {}
        if headers:
            kw["headers"] = headers
        # Provide a referer if given
        if referer:
            headers_to_send = dict(scraper.headers)
            headers_to_send.update({"Referer": referer})
            if headers:
                headers_to_send.update(headers)
            kw["headers"] = headers_to_send
        return scraper.get(url, timeout=30, **kw)

    resp = await loop.run_in_executor(None, do_get)
    debug_print_response(resp, note=f"fetch_response -> {url}")
    return resp


async def fetch_text(url: str, referer: Optional[str] = None, headers: Optional[dict] = None) -> str:
    resp = await fetch_response(url, referer=referer, headers=headers)
    return resp.text or ""


async def fetch_json(url: str, referer: Optional[str] = None, headers: Optional[dict] = None) -> dict:
    resp = await fetch_response(url, referer=referer, headers=headers)
    try:
        return resp.json()
    except Exception as e:
        print("❌ Error decoding JSON response:", e)
        # Log snippet in case the server returned HTML instead
        print("🔍 Non-JSON response snippet:", (resp.text or "")[:800])
        raise


# --- Utility to build browser-like headers for specific requests ---
def build_headers(referer: Optional[str] = None, origin: Optional[str] = None, mobile: bool = True) -> dict:
    ua = USER_AGENT_MOBILE if mobile else USER_AGENT_DESKTOP
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        h["Referer"] = referer
    if origin:
        h["Origin"] = origin
    return h


# --- API Endpoints ---


@app.get("/search", summary="Search for anime by title query (includes thumbnails)")
async def search_anime(title: str):
    if not title:
        raise HTTPException(status_code=400, detail="Title query must be provided.")

    global base_referer
    safe_title = quote(title)
    search_url = f"{BASE_URL}/archivio?title={safe_title}"

    print(f"\n➡️ /search called with title={title!r}, search_url={search_url}")
    # Use current referer if available
    headers = build_headers(referer=base_referer, origin=BASE_URL, mobile=True)

    try:
        resp = await fetch_response(search_url, referer=base_referer, headers=headers)
        # If Cloudflare returns 403 or a challenge, fetch_response logged it
        if resp.status_code == 403:
            # try alternate UA (desktop) once more
            print("⚠️ Received 403 on search; retrying with desktop UA")
            headers2 = build_headers(referer=base_referer, origin=BASE_URL, mobile=False)
            resp = await fetch_response(search_url, referer=base_referer, headers=headers2)

        html = resp.text or ""
        # Update referer to the actual response URL (helps mimic browser navigation)
        base_referer = str(resp.url)

        # Log cookies currently stored
        print("🍪 Current session cookies:", scraper.cookies.get_dict())

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

    except HTTPException:
        raise
    except Exception as e:
        print("❌ /search fatal error:", e)
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")


@app.get("/episodes", summary="Get episodes for a specific anime ID")
async def get_episodes(anime_id: int):
    global base_referer
    info_url = f"{BASE_URL}/info_api/{anime_id}/0"
    print(f"\n➡️ /episodes called for anime_id={anime_id}, url={info_url}")
    headers = build_headers(referer=base_referer, origin=BASE_URL, mobile=True)
    try:
        info_resp = await fetch_response(info_url, referer=base_referer, headers=headers)
        if info_resp.status_code == 403:
            print("⚠️ Received 403 on info_api; retrying with desktop UA")
            info_resp = await fetch_response(info_url, referer=base_referer, headers=build_headers(referer=base_referer, origin=BASE_URL, mobile=False))

        base_referer = str(info_resp.url)
        print("🍪 Cookies after info_api:", scraper.cookies.get_dict())
        info_data = info_resp.json()
        count = info_data.get("episodes_count", 0)
        if count == 0:
            return {"anime_id": anime_id, "episodes": []}

        fetch_url_episodes = f"{info_url}?start_range=0&end_range={min(count, 120)}"
        episodes_resp = await fetch_response(fetch_url_episodes, referer=base_referer, headers=headers)
        base_referer = str(episodes_resp.url)
        episodes_data = episodes_resp.json()
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
    except HTTPException:
        raise
    except Exception as e:
        print("❌ /episodes fatal error:", e)
        raise HTTPException(status_code=500, detail=f"Failed to get episodes: {e}")


@app.get("/stream", summary="Get direct video URL (cached & retried)")
async def get_stream_url(episode_id: int):
    global base_referer
    now = time.time()
    cached = stream_cache.get(episode_id)
    if cached and now - cached["timestamp"] < CACHE_TTL:
        return {"episode_id": episode_id, "stream_url": cached["url"], "cached": True}

    embed_url_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    print(f"\n➡️ /stream called for episode_id={episode_id}, url={embed_url_endpoint}")
    try:
        embed_resp = await fetch_response(embed_url_endpoint, referer=base_referer, headers=build_headers(referer=base_referer, origin=BASE_URL))
        if embed_resp.status_code == 403:
            print("⚠️ Received 403 on embed-url; retrying with desktop UA")
            embed_resp = await fetch_response(embed_url_endpoint, referer=base_referer, headers=build_headers(referer=base_referer, origin=BASE_URL, mobile=False))

        # Some endpoints redirect (Location), some return URL in body
        if embed_resp.status_code in (301, 302) and "location" in embed_resp.headers:
            embed_target_url = embed_resp.headers.get("location")
        else:
            embed_target_url = (embed_resp.text or "").strip()

        print("🍪 Cookies while resolving embed:", scraper.cookies.get_dict())
        if not embed_target_url or not embed_target_url.startswith("http"):
            print("❌ Invalid embed target URL:", embed_target_url[:200])
            raise HTTPException(status_code=500, detail="Invalid embed target URL")

        video_page_resp = await fetch_response(embed_target_url, referer=embed_url_endpoint, headers=build_headers(referer=embed_url_endpoint, origin=BASE_URL))
        video_page_resp.raise_for_status()
        video_url = extract_video_url_from_embed_html(video_page_resp.text)
        if not video_url:
            print("❌ No video URL found in embed page. HTML snippet:")
            print((video_page_resp.text or "")[:1200])
            raise HTTPException(status_code=404, detail="No video URL found")

        stream_cache[episode_id] = {"url": video_url, "timestamp": now}
        base_referer = str(video_page_resp.url)
        return {"episode_id": episode_id, "stream_url": video_url, "cached": False}

    except HTTPException:
        raise
    except Exception as e:
        print("❌ /stream fatal error:", e)
        raise HTTPException(status_code=500, detail=f"Failed to get stream URL: {e}")


@app.get("/stream_video", summary="Stream video file (with retries)")
async def stream_video(request: Request, episode_id: int):
    global scraper
    if not scraper:
        raise HTTPException(status_code=500, detail="Scraper not initialized")

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

    # Synchronous generator that streams via requests (cloudscraper)
    def sync_stream_gen():
        hdrs = {}
        if range_header:
            hdrs["Range"] = f"bytes={range_start}-" if not range_end else f"bytes={range_start}-{range_end}"
        # cloudscraper returns a requests.Response that supports iter_content
        with scraper.get(stream_url, headers=hdrs, stream=True, timeout=30) as r:
            if r.status_code not in (200, 206):
                print("❌ Video fetch returned status:", r.status_code)
                raise HTTPException(status_code=500, detail="Failed to fetch video")
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    yield chunk

    # Determine content-length (best-effort)
    try:
        head = scraper.head(stream_url, timeout=20)
        content_length = int(head.headers.get("content-length", 0))
    except Exception:
        content_length = 0

    headers_to_send = {"Content-Type": "video/mp4", "Accept-Ranges": "bytes"}
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

    return StreamingResponse(sync_stream_gen(), headers=headers_to_send, status_code=status_code)
