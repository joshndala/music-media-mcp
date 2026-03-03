"""
Music Media MCP Server
======================
An MCP server that generates AI music videos using:
  - Vertex AI Lyria 3 (audio generation)
  - FFmpeg (media merging)
  - Google Cloud Storage (output hosting)

Environment Variables:
  GCP_PROJECT_ID   - Google Cloud project ID (required)
  GCS_BUCKET_NAME  - GCS bucket for output uploads (required)
  GCP_LOCATION     - Vertex AI region (default: us-central1)
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import os
import subprocess
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import ffmpeg
import google.auth
import google.auth.transport.requests
import httpx
from google.cloud import storage
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "")

LYRIA_MODEL = "lyria-002"
GEMINI_MODEL = "gemini-2.5-flash"
AUDIO_DURATION_SEC = 30  # Lyria generates ~30s clips

VERTEX_AI_ENDPOINT = (
    f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
    f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
    f"publishers/google/models/{LYRIA_MODEL}:predict"
)

GEMINI_ENDPOINT = (
    f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
    f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
    f"publishers/google/models/{GEMINI_MODEL}:generateContent"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("music-media-mcp")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Music Media MCP",
    instructions=(
        "Generates AI music videos by combining Vertex AI Lyria 3 audio "
        "with a source image or video, then uploads the result to GCS."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_access_token() -> str:
    """Get a valid GCP access token using Application Default Credentials."""
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_request = google.auth.transport.requests.Request()
    credentials.refresh(auth_request)
    return credentials.token


def _detect_media_type(file_path: str, content_type: str | None) -> str:
    """
    Determine whether the downloaded file is an 'image' or 'video'.
    Returns 'image' or 'video'.
    """
    image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
    video_extensions = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"}

    ext = Path(file_path).suffix.lower()
    if ext in image_extensions:
        return "image"
    if ext in video_extensions:
        return "video"

    # Fallback to content-type
    if content_type:
        ct = content_type.lower()
        if ct.startswith("image/"):
            return "image"
        if ct.startswith("video/"):
            return "video"

    # Default to video if we can't determine
    return "video"


def _get_video_duration(file_path: str) -> float:
    """Probe a video file and return its duration in seconds."""
    try:
        probe = ffmpeg.probe(file_path)
        duration = float(probe["format"]["duration"])
        return duration
    except (ffmpeg.Error, KeyError, ValueError) as exc:
        logger.warning("Could not probe video duration: %s — defaulting to 0", exc)
        return 0.0


def _extract_first_frame(video_path: str, dest_dir: str) -> str:
    """Extract the first frame of a video as a JPEG for Gemini analysis."""
    frame_path = os.path.join(dest_dir, "first_frame.jpg")
    (
        ffmpeg
        .input(video_path)
        .output(frame_path, vframes=1, format="image2", vcodec="mjpeg")
        .overwrite_output()
        .run(capture_stdout=True, capture_stderr=True)
    )
    return frame_path


def _analyze_media_for_music_prompt(file_path: str, media_type: str, dest_dir: str) -> str:
    """
    Use Gemini Vision to analyze the source media and generate a
    Lyria-compatible music prompt that fits the visual content.
    """
    logger.info("No music prompt provided — asking Gemini to analyze the media...")

    # For video, extract a representative frame
    if media_type == "video":
        try:
            image_path = _extract_first_frame(file_path, dest_dir)
        except ffmpeg.Error:
            logger.warning("Could not extract video frame — using text-only Gemini prompt.")
            image_path = None
    else:
        image_path = file_path

    # Build the Gemini request
    text_part = {
        "text": (
            "You are a music supervisor. Analyze this image and write a detailed "
            "music generation prompt for Google Lyria that would perfectly complement it. "
            "Describe the mood, genre, instrumentation, tempo, and atmosphere. "
            "Be specific and evocative. Do NOT reference any specific copyrighted songs or artists. "
            "Return only the prompt text, nothing else."
        )
    }

    parts = []
    if image_path:
        mime_type = "image/jpeg"
        if image_path.lower().endswith(".png"):
            mime_type = "image/png"
        elif image_path.lower().endswith(".webp"):
            mime_type = "image/webp"

        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        parts.append({"inlineData": {"mimeType": mime_type, "data": image_b64}})

    parts.append(text_part)

    request_body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 256},
    }

    access_token = _get_access_token()

    with httpx.Client(timeout=60) as client:
        response = client.post(
            GEMINI_ENDPOINT,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API error (HTTP {response.status_code}): {response.text}"
            )

    result = response.json()
    try:
        music_prompt = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Unexpected Gemini response structure: {exc}\nResponse: {result}"
        ) from exc

    logger.info("Gemini generated music prompt: '%s'", music_prompt[:120])
    return music_prompt


def _download_source_media(url: str, dest_dir: str) -> tuple[str, str]:
    """
    Download the source media from a URL.
    Returns (local_file_path, media_type) where media_type is 'image' or 'video'.
    """
    logger.info("Downloading source media from: %s", url)

    parsed = urlparse(url)
    filename = Path(parsed.path).name or "source_media"
    # Ensure the filename has a reasonable extension
    if not Path(filename).suffix:
        filename += ".mp4"

    local_path = os.path.join(dest_dir, filename)

    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")

        with open(local_path, "wb") as f:
            f.write(response.content)

    media_type = _detect_media_type(local_path, content_type)
    logger.info("Downloaded %s (%s) -> %s", media_type, content_type, local_path)
    return local_path, media_type


def _generate_audio(music_prompt: str, dest_dir: str) -> str:
    """
    Call Vertex AI Lyria 3 to generate a 30-second audio clip.
    Returns the path to the saved WAV file.
    """
    if not GCP_PROJECT_ID:
        raise ValueError(
            "GCP_PROJECT_ID environment variable is required but not set."
        )

    logger.info("Generating audio with Lyria 3: '%s'", music_prompt[:80])

    access_token = _get_access_token()

    request_body = {
        "instances": [{"prompt": music_prompt}],
        "parameters": {},
    }

    with httpx.Client(timeout=120) as client:
        response = client.post(
            VERTEX_AI_ENDPOINT,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Vertex AI Lyria API error (HTTP {response.status_code}): "
                f"{response.text}"
            )

    result = response.json()

    # Extract the base64-encoded audio from the response
    try:
        predictions = result["predictions"]
        audio_b64 = predictions[0]["bytesBase64Encoded"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Unexpected Lyria API response structure: {exc}\n"
            f"Response keys: {list(result.keys())}"
        ) from exc

    audio_bytes = base64.b64decode(audio_b64)
    audio_path = os.path.join(dest_dir, "lyria_output.wav")

    with open(audio_path, "wb") as f:
        f.write(audio_bytes)

    logger.info("Audio generated: %s (%.1f KB)", audio_path, len(audio_bytes) / 1024)
    return audio_path


def _merge_media(
    source_path: str,
    media_type: str,
    audio_path: str,
    dest_dir: str,
) -> str:
    """
    Merge the source media with the generated audio using FFmpeg.

    - Image: loop for AUDIO_DURATION_SEC seconds
    - Video <= AUDIO_DURATION_SEC: loop to fill duration
    - Video > AUDIO_DURATION_SEC: trim to AUDIO_DURATION_SEC

    Resolution is preserved from the source. A scale filter ensures
    dimensions are divisible by 2 (required by H.264 / yuv420p).
    """
    output_path = os.path.join(dest_dir, "output.mp4")
    duration = AUDIO_DURATION_SEC

    # The scale filter keeps original dimensions but rounds odd values
    # to the nearest even number (required for H.264 + yuv420p).
    # -2 means "keep aspect ratio, round to nearest even".
    EVEN_SCALE_FILTER = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    logger.info("Merging %s with audio (target: %ds)", media_type, duration)

    audio_input = ffmpeg.input(audio_path)

    if media_type == "image":
        # Loop the image for the audio duration
        video_input = ffmpeg.input(
            source_path,
            loop=1,
            framerate=24,
            t=duration,
        )
        (
            ffmpeg.output(
                video_input.filter("scale", "trunc(iw/2)*2", "trunc(ih/2)*2"),
                audio_input,
                output_path,
                vcodec="libx264",
                acodec="aac",
                pix_fmt="yuv420p",
                shortest=None,
                t=duration,
            )
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
    else:
        # It's a video — probe the source resolution for logging
        try:
            probe = ffmpeg.probe(source_path)
            video_streams = [
                s for s in probe.get("streams", [])
                if s.get("codec_type") == "video"
            ]
            if video_streams:
                src_w = video_streams[0].get("width", "?")
                src_h = video_streams[0].get("height", "?")
                logger.info("Source video resolution: %sx%s", src_w, src_h)
        except ffmpeg.Error:
            pass

        video_duration = _get_video_duration(source_path)

        if video_duration <= 0:
            video_input = ffmpeg.input(source_path, t=duration)
        elif video_duration < duration:
            video_input = ffmpeg.input(
                source_path,
                stream_loop=-1,
                t=duration,
            )
        else:
            video_input = ffmpeg.input(source_path, t=duration)

        (
            ffmpeg.output(
                video_input.video.filter("scale", "trunc(iw/2)*2", "trunc(ih/2)*2"),
                audio_input,
                output_path,
                vcodec="libx264",
                acodec="aac",
                pix_fmt="yuv420p",
                movflags="+faststart",
                t=duration,
            )
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

    file_size = os.path.getsize(output_path)
    logger.info("Merge complete: %s (%.1f MB)", output_path, file_size / 1024 / 1024)
    return output_path


def _upload_to_gcs(local_path: str) -> str:
    """
    Upload the merged video to GCS and return an accessible URL.

    Tries to generate a 24-hour signed URL (requires service account).
    Falls back to a public URL if signing isn't available.
    """
    if not GCS_BUCKET_NAME:
        raise ValueError(
            "GCS_BUCKET_NAME environment variable is required but not set."
        )

    blob_name = f"generated/{uuid.uuid4().hex}.mp4"
    logger.info("Uploading to gs://%s/%s", GCS_BUCKET_NAME, blob_name)

    client = storage.Client(project=GCP_PROJECT_ID)
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(blob_name)

    blob.upload_from_filename(local_path, content_type="video/mp4")

    # Try signed URL first (requires service account credentials)
    try:
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=24),
            method="GET",
        )
        logger.info("Upload complete. Signed URL generated (24h expiry).")
        return signed_url
    except Exception as sign_err:
        logger.warning("Signed URL failed (%s) — falling back to public URL.", sign_err)

    # Fallback: return the direct public URL.
    # Requires the bucket to have allUsers=Storage Object Viewer in IAM.
    public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{blob_name}"
    logger.info("Upload complete. Public URL: %s", public_url)
    return public_url


# ---------------------------------------------------------------------------
# MCP Tool
# ---------------------------------------------------------------------------


@mcp.tool()
def generate_and_merge_media(
    source_media_url: str,
    music_prompt: str | None = None,
) -> str:
    """
    Generates a music video by combining AI-generated audio with the source image or video.

    Args:
        source_media_url: A publicly accessible URL to a source image or video.
        music_prompt: Optional music description. If omitted, Gemini generates one.

    IMPORTANT: The output of this tool is valid HTML. 
    You MUST display this output to the user by generating a generic HTML Artifact 
    containing the exact code returned by the tool.
    """
    # Sanitize inputs (MCP Inspector sometimes adds trailing newlines)
    source_media_url = source_media_url.strip()
    if music_prompt:
        music_prompt = music_prompt.strip()

    tmp_dir = tempfile.mkdtemp(prefix="music_media_mcp_")

    try:
        # Step 1: Download source media
        source_path, media_type = _download_source_media(
            source_media_url, tmp_dir
        )

        # Step 2: Determine music prompt (user-provided or Gemini-generated)
        if not music_prompt:
            music_prompt = _analyze_media_for_music_prompt(
                source_path, media_type, tmp_dir
            )
        else:
            logger.info("Using user-provided music prompt: '%s'", music_prompt[:80])

        # Step 3: Generate audio via Vertex AI Lyria 3
        audio_path = _generate_audio(music_prompt, tmp_dir)

        # Step 4: Merge media with FFmpeg
        output_path = _merge_media(
            source_path, media_type, audio_path, tmp_dir
        )

        # Step 5: Upload to GCS & get URL
        result_url = _upload_to_gcs(output_path)

        # Step 6: Return a React artifact so the client renders an interactive
        # video player. Claude and compatible clients will render the
        # <antml-artifact> tag as a rich UI component automatically.
        logger.info("Pipeline complete. Returning artifact.")
        return _build_artifact(result_url, music_prompt)

    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        raise

    finally:
        # Cleanup: remove all temporary files
        import shutil

        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("Cleaned up temp directory: %s", tmp_dir)
        except Exception:
            pass


def _build_artifact(video_url: str, music_prompt: str) -> str:
    """
    Build a simple HTML page with a video player and download link.
    Follows the same pattern as the movie-battle MCP.
    """
    safe_prompt = music_prompt.replace('"', '&quot;').replace('<', '&lt;')
    short_prompt = safe_prompt[:120] + ('...' if len(safe_prompt) > 120 else '')

    return f"""<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 20px; background: #0d0d0d; }}
        .card {{ max-width: 580px; margin: 0 auto; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); border-radius: 16px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.4); }}
        video {{ width: 100%; display: block; }}
        .info {{ padding: 16px 20px 20px; text-align: center; }}
        .prompt {{ font-size: 13px; color: #94a3b8; font-style: italic; margin-bottom: 4px; }}
        .sublabel {{ font-size: 11px; color: #4b5563; margin-bottom: 12px; }}
        .tip {{ font-size: 12px; color: #64748b; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 10px 14px; line-height: 1.5; }}
        .tip strong {{ color: #94a3b8; }}
    </style>
</head>
<body>
    <div class="card">
        <video controls autoplay src="{video_url}">
            Your browser does not support the video tag.
        </video>
        <div class="info">
            <p class="prompt">🎵 {short_prompt}</p>
            <p class="sublabel">AI-generated music video</p>
            <p class="tip">💡 <strong>To download:</strong> Click the ⋮ or ⋯ menu on the video player and select <strong>Download</strong>.</p>
        </div>
    </div>
</body>
</html>
"""






# ---------------------------------------------------------------------------

# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server with the specified transport."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"],
                        help="Choose 'stdio' for Desktop Apps or 'sse' for Web/ChatGPT")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), 
                        help="Port for SSE (defaults to 8000 or PORT env var)")
    
    args = parser.parse_args()

    if args.transport == "sse":
        # FastMCP uses host/port from settings if transport is sse
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = args.port
        print(f"🚀 Starting Server (SSE) on port {mcp.settings.port}...")
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
