from fastapi import FastAPI
from contextlib import asynccontextmanager
import httpx
import time
import asyncio
from bs4 import BeautifulSoup
import json
import re

BASE_URL = "https://www.animeunity.so"
CACHE_TTL = 300
MAX_RETRIES = 3
RETRY_DELAY = 2

stream_cache = {}
shared_client: httpx.AsyncClient | None = None
base_referer = BASE_URL

# --- Lifespan handler ---
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

    # Startup logic
    try:
        initial_resp = await shared_client.get(BASE_URL)
        initial_resp.raise_for_status()
        base_referer = str(initial_resp.url)
        print("✅ Shared HTTP client initialized")
        print("🔐 Initial cookies fetched:", shared_client.cookies)
        print("📨 Initial headers:", shared_client.headers)
    except Exception as e:
        print(f"❌ Error fetching base URL: {e}")

    # Yield control to allow the app to run
    yield

    # Shutdown logic
    if shared_client:
        await shared_client.aclose()
        print("🧹 Shared HTTP client closed")


# Create app with lifespan handler
app = FastAPI(
    title="AnimeUnity API Proxy",
    description="Async API to interact with AnimeUnity using httpx and shared sessions.",
    version="2.1.1",
    lifespan=lifespan
)
