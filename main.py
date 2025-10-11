from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import StreamingResponse
import requests
from urllib.parse import quote
import json
import re
import httpx
import math

# --- Configuration ---
BASE_URL = "https://www.animeunity.so"

app = FastAPI(
    title="AnimeUnity API Proxy",
    description="API to interact with AnimeUnity based on provided internal data structure.",
    version="1.0.0"
)

# --- Helper Functions ---

def extract_json_from_html(html_content: str) -> list:
    try:
        match = re.search(r'<archivio records="([^"]+)"', html_content)
        if match:
            escaped_json = match.group(1)
            unescaped_json = escaped_json.replace("&quot;", '"')
            return json.loads(unescaped_json)
        return []
    except Exception as e:
        print(f"Error extracting or parsing JSON: {e}")
        return []

def extract_video_url_from_embed_html(html_content: str) -> str | None:
    try:
        match = re.search(r"window\.downloadUrl\s*=\s*'([^']+)'", html_content)
        if match:
            return match.group(1)
        return None
    except Exception as e:
        print(f"Error extracting video URL: {e}")
        return None

# --- API Endpoints ---

@app.get("/search", summary="Search for anime by title query")
async def search_anime(title: str):
    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Title query must be provided."
        )
    safe_title = quote(title)
    search_url = f"{BASE_URL}/archivio?title={safe_title}"
    try:
        response = requests.get(search_url)
        response.raise_for_status()
        anime_records = extract_json_from_html(response.text)
        cleaned_records = []
        for record in anime_records:
            cleaned_record = {
                "id": record.get("id"),
                "title_en": record.get("title_eng", record.get("title")),
                "title_it": record.get("title_it", record.get("title")),
                "type": record.get("type"),
                "status": record.get("status"),
                "date": record.get("date"),
                "episodes_count": record.get("episodes_count"),
                "score": record.get("score"),
                "studio": record.get("studio"),
                "slug": record.get("slug"),
                "plot": record.get("plot").strip(),
                "genres": [g.get("name") for g in record.get("genres", [])]
            }
            cleaned_records.append(cleaned_record)
        return cleaned_records
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"HTTP Error during search: {e}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {e}"
        )

@app.get("/episodes", summary="Get a list of episodes for a specific anime ID")
async def get_episodes(anime_id: int):
    info_url = f"{BASE_URL}/info_api/{anime_id}/0"
    try:
        info_response = requests.get(info_url)
        info_response.raise_for_status()
        info_data = info_response.json()
        episodes_count = info_data.get("episodes_count", 0)
        if episodes_count == 0:
            return {"anime_id": anime_id, "episodes_count": 0, "episodes": []}
        fetch_url = f"{info_url}?start_range=0&end_range={min(episodes_count, 120)}"
        episodes_response = requests.get(fetch_url)
        episodes_response.raise_for_status()
        episodes_data = episodes_response.json()
        cleaned_episodes = []
        for episode in episodes_data.get("episodes", []):
            cleaned_episode = {
                "episode_id": episode.get("id"),
                "number": episode.get("number"),
                "created_at": episode.get("created_at"),
                "visits": episode.get("visite"),
                "scws_id": episode.get("scws_id")
            }
            cleaned_episodes.append(cleaned_episode)
        return {
            "anime_id": anime_id,
            "episodes_count": episodes_count,
            "episodes": cleaned_episodes
        }
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"HTTP Error while fetching episodes: {e}"
        )
    except requests.exceptions.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to parse JSON response from episode API."
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {e}"
        )

@app.get("/stream", summary="Get the direct video file URL for an episode ID")
async def get_stream_url(episode_id: int):
    embed_url_endpoint = f"{BASE_URL}/embed-url/{episode_id}"
    try:
        embed_response = requests.get(embed_url_endpoint, allow_redirects=False)
        embed_response.raise_for_status()
        if embed_response.status_code == 302:
            embed_target_url = embed_response.headers.get("Location")
        else:
            embed_target_url = embed_response.text.strip()
            if not embed_target_url.startswith('http'):
                raise ValueError("Could not find a valid embed URL.")
        if not embed_target_url:
            raise ValueError("Could not find embed URL in response.")
        video_page_response = requests.get(embed_target_url)
        video_page_response.raise_for_status()
        video_download_url = extract_video_url_from_embed_html(video_page_response.text)
        if not video_download_url:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Direct video download URL not found on the embed page."
            )
        return {
            "episode_id": episode_id,
            "embed_url": embed_target_url,
            "stream_url": video_download_url
        }
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"HTTP Error while fetching stream information: {e}"
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {e}"
        )

# --- Stream video with proper Range support ---
@app.get("/stream_video", summary="Stream the video file for an episode ID")
async def stream_video(request: Request, episode_id: int):
    # Step 1: Get fresh stream URL
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            resp = await client.get(f"http://127.0.0.1:8000/stream?episode_id={episode_id}")
            resp.raise_for_status()
            data = resp.json()
            stream_url = data.get("stream_url")
            if not stream_url:
                raise HTTPException(status_code=404, detail="Stream URL not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to get fresh stream URL: {e}")

    # Step 2: Parse Range header for HTML5 video seeking
    range_header = request.headers.get("range")
    range_start = 0
    range_end = None
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            range_start = int(match.group(1))
            if match.group(2):
                range_end = int(match.group(2))

    async def video_generator():
        async with httpx.AsyncClient(timeout=None) as client:
            stream_headers = {"Range": f"bytes={range_start}-{range_end}" if range_end else f"bytes={range_start}-"}
            async with client.stream("GET", stream_url, headers=stream_headers) as video_resp:
                if video_resp.status_code not in (200, 206):
                    raise HTTPException(status_code=video_resp.status_code, detail="Failed to fetch video")
                async for chunk in video_resp.aiter_bytes(1024 * 1024):
                    yield chunk

    # Step 3: Fetch content length to calculate Content-Range if possible
    content_length = None
    async with httpx.AsyncClient(timeout=None) as client:
        head_resp = await client.head(stream_url)
        if head_resp.status_code == 200 and "content-length" in head_resp.headers:
            content_length = int(head_resp.headers["content-length"])

    headers_to_send = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
    }

    if content_length is not None and range_header:
        end = content_length - 1 if range_end is None else range_end
        headers_to_send["Content-Range"] = f"bytes {range_start}-{end}/{content_length}"
        headers_to_send["Content-Length"] = str(end - range_start + 1)
        status_code = 206
    elif content_length is not None:
        headers_to_send["Content-Length"] = str(content_length)
        status_code = 200
    else:
        status_code = 200

    return StreamingResponse(video_generator(), headers=headers_to_send, status_code=status_code)
