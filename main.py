import asyncio
import time
from typing import Any, Dict, Optional
import cloudscraper
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="AnimeUnity Scraper API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global scraper + cache
scraper = None
scraper_lock = asyncio.Lock()
last_referer = None
cached_homepage = None
cached_cookies = None

BASE_URL = "https://www.animeunity.so"
STREAM_CACHE_TTL = 60 * 15  # 15 minutes
stream_cache: Dict[int, Dict[str, Any]] = {}


# ---------------------------- Startup/Shutdown ----------------------------
@app.on_event("startup")
async def startup_event():
    global scraper, cached_cookies
    print("🚀 Initializing cloudscraper client...")

    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})

    # HEAD request for warmup (like browser does)
    try:
        print("🌐 Sending HEAD request to warm Cloudflare cache...")
        scraper.head(BASE_URL, timeout=10)
    except Exception as e:
        print(f"⚠️ HEAD warmup failed: {e}")

    # Store initial cookies
    try:
        scraper.get(BASE_URL, timeout=10)
        cached_cookies = scraper.cookies
        print(f"✅ Cookies cached: {len(cached_cookies)} cookies")
    except Exception as e:
        print(f"⚠️ Initial cookie fetch failed: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    global scraper
    print("🛑 Shutting down scraper client...")
    scraper = None


# ---------------------------- Helpers ----------------------------
def cloudscraper_get(url: str, referer: Optional[str] = None, as_json: bool = False):
    """Perform GET requests through cloudscraper with referer + retry"""
    global last_referer
    headers = {
        "User-Agent": scraper.headers.get("User-Agent"),
        "Referer": referer or BASE_URL,
        "Origin": BASE_URL,
        "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    }

    for attempt in range(3):
        try:
            res = scraper.get(url, headers=headers, timeout=15)
            if res.status_code == 403:
                print(f"⚠️ Got 403 on attempt {attempt + 1}, refreshing scraper...")
                time.sleep(2)
                scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
                continue
            last_referer = url
            return res.json() if as_json else res.text
        except Exception as e:
            print(f"⚠️ Retry {attempt + 1} failed for {url}: {e}")
            time.sleep(2)
    raise HTTPException(status_code=500, detail=f"Failed to fetch {url}")


# ---------------------------- Models ----------------------------
class SearchResult(BaseModel):
    title: str
    link: str
    image: Optional[str] = None
    description: Optional[str] = None


# ---------------------------- Endpoints ----------------------------
@app.get("/search", response_model=list[SearchResult])
async def search_anime(title: str):
    """Search anime on AnimeUnity"""
    url = f"{BASE_URL}/archivio?title={title}"
    print(f"🔍 Searching for '{title}' at {url}")
    html = cloudscraper_get(url)
    if "Nessun risultato" in html:
        return []
    results = []
    for line in html.splitlines():
        if 'href="/anime/' in line:
            parts = line.split('"')
            for i, p in enumerate(parts):
                if p.startswith("/anime/"):
                    link = BASE_URL + p
                    title = parts[i + 2] if i + 2 < len(parts) else "Unknown"
                    results.append(SearchResult(title=title.strip(), link=link))
    return results


@app.get("/details")
async def anime_details(url: str):
    """Get anime details"""
    print(f"📄 Fetching details for {url}")
    html = cloudscraper_get(url)
    details = {
        "title": "Unknown",
        "episodes": [],
        "description": None,
    }
    lines = html.splitlines()
    for line in lines:
        if "<h1" in line and "</h1>" in line:
            details["title"] = line.strip().split(">")[1].split("<")[0]
        if "/watch/" in line:
            details["episodes"].append(BASE_URL + line.split('"')[1])
    return details


@app.get("/stream")
async def get_stream_url(episode_id: int):
    """Use httpx only for video endpoints"""
    global last_referer

    now = time.time()
    if episode_id in stream_cache and now - stream_cache[episode_id]["timestamp"] < STREAM_CACHE_TTL:
        return stream_cache[episode_id]["data"]

    video_url = f"{BASE_URL}/ajax/episode/info?id={episode_id}"
    print(f"🎬 Fetching stream info for episode {episode_id}")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(video_url, headers={"Referer": last_referer or BASE_URL})
        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail="Failed to fetch stream URL")

    data = res.json()
    stream_cache[episode_id] = {"data": data, "timestamp": now}
    return data


# ---------------------------- Root ----------------------------
@app.get("/")
async def root():
    return {"status": "ok", "message": "AnimeUnity Scraper API is running 🚀"}


# ---------------------------- Main ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)