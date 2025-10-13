# AnimeUnity API

This is a FastAPI-based proxy for AnimeUnity that allows you to:

1. Search anime by title.
2. Retrieve episodes for a specific anime.
3. Get direct video stream URLs for episodes.
4. Stream videos directly via `/stream_video` with full HTML5 `<video>` support.

## Features

- Async streaming with `httpx` for efficient video delivery.
- Supports `Range` requests for HTML5 `<video>` playback.
- Provides JSON endpoints for search and episode listing.

## Endpoints

1. **Search Anime**
GET /search?title={title}  
Returns a JSON list of matching anime.

2. **Get Episodes**
GET /episodes?anime_id={anime_id}  
Returns a JSON list of episodes for a given anime ID.

3. **Get Stream URL**
GET /stream?episode_id={episode_id}  
Returns a JSON object with the direct video download URL.

4. **Stream Video**
GET /stream_video?episode_id={episode_id}  
Streams the video directly to the browser. Supports HTML5 `<video>` seeking.

## Installation

1. Clone the repository:
```
git clone https://github.com/Pal-droid/Animeunity-API
cd Animeunity-API
```

2. Install dependencies:
```
pip install -r requirements.txt
```

3. Run the API:
```
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

4. Access endpoints via:  
http://127.0.0.1:8000/docs  
to see the Swagger UI documentation.

## Usage Example

HTML5 video playback:
```
<video controls width="640" height="360">
  <source src="http://127.0.0.1:8000/stream_video?episode_id=71592" type="video/mp4">
</video>
```

The video will stream directly from the AnimeUnity source and supports seeking.

# Notes

For some reason in some deployed instances the API gets 403'd *(So far ive only seen it happen with [Render](https://render.com))*, im still looking into it, if you need it to work then a working service would be [Koyeb](https://koyeb.com)
