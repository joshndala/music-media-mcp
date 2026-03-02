# 🎵 Music Media MCP Server

An MCP (Model Context Protocol) server that generates AI-powered music videos. Give it an image or video and it will analyze the visual content, compose a matching soundtrack using Google's Lyria 3 model, merge everything with FFmpeg, and return a playable video artifact.

## Pipeline

```
Source Media (image/video URL)
  → Gemini Vision analyzes the visual content (if no prompt given)
  → Lyria 3 generates a 30-second AI music track
  → FFmpeg merges audio + media into a single .mp4
  → Uploads to Google Cloud Storage
  → Returns an HTML artifact with an inline video player
```

## Features

- **Auto music prompting** — If no music description is provided, Gemini Vision analyzes the image/video and generates a fitting music prompt automatically
- **Multiple media types** — Supports images (.jpg, .png, .webp) and videos (.mp4, .mov)
- **Smart video handling** — Images loop for 30s, short videos loop to fill, long videos trim to 30s
- **HTML artifact output** — Returns a styled video player that MCP-compatible chatbots render inline
- **Cloud Run ready** — Deploys to Google Cloud Run with a single command

## Prerequisites

- **Python 3.10+**
- **FFmpeg** installed and on `PATH`
  ```bash
  # macOS
  brew install ffmpeg
  # Ubuntu/Debian
  sudo apt install ffmpeg
  ```
- **Google Cloud** project with:
  - Vertex AI API enabled (Lyria `lyria-002` + Gemini `gemini-2.0-flash-001`)
  - A GCS bucket for output storage (with public read access or signed URLs)
  - Application Default Credentials:
    ```bash
    gcloud auth application-default login
    ```

## Setup

1. **Clone and install:**
   ```bash
   git clone https://github.com/joshndala/music-media-mcp.git
   cd music-media-mcp
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your GCP project ID and GCS bucket name
   ```

3. **Set up GCS CORS** (required for video playback in chatbot artifacts):
   ```bash
   # Create cors.json
   echo '[{"origin":["*"],"method":["GET"],"responseHeader":["Content-Type","Content-Length","Range"],"maxAgeSeconds":3600}]' > cors.json
   gsutil cors set cors.json gs://YOUR_BUCKET_NAME
   ```

## Running Locally

```bash
# stdio transport (for Claude Desktop and other MCP desktop clients)
python server.py

# SSE transport (for web-based MCP clients)
python server.py --transport sse --port 8000

# Test with MCP Inspector
npx @modelcontextprotocol/inspector
# Then connect to http://localhost:8000/sse
```

## Deploying to Cloud Run

```bash
# Build the container
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/YOUR_PROJECT/YOUR_REPO/music-media-server \
  --project YOUR_PROJECT

# Deploy
gcloud run deploy music-media-server \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT/YOUR_REPO/music-media-server \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=YOUR_PROJECT,GCS_BUCKET_NAME=YOUR_BUCKET,GCP_LOCATION=us-central1" \
  --memory 2Gi \
  --timeout 300 \
  --project YOUR_PROJECT
```

Your SSE endpoint will be at: `https://YOUR_SERVICE_URL/sse`

## MCP Client Configuration

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "music-media": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py", "--transport", "stdio"],
      "env": {
        "GCP_PROJECT_ID": "your-project-id",
        "GCS_BUCKET_NAME": "your-bucket-name",
        "GCP_LOCATION": "us-central1"
      }
    }
  }
}
```

### Web/Chatbot (SSE)

Point your MCP client to your deployed Cloud Run URL:
```
https://your-service-url.run.app/sse
```

## Tool Reference

### `generate_and_merge_media`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source_media_url` | `string` | ✅ | Direct URL to a source image or video |
| `music_prompt` | `string` | ❌ | Music style description (auto-generated if omitted) |

**Returns:** A complete HTML document with an inline video player.

**Example prompts:**
- `"Upbeat electronic dance music with synth arpeggios"`
- `"Calm ambient piano piece evoking a misty morning"`
- `"Cinematic orchestral score with soaring strings"`
- _(omit for automatic AI analysis)_

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GCP_PROJECT_ID` | ✅ | — | Google Cloud project ID |
| `GCS_BUCKET_NAME` | ✅ | — | GCS bucket for video uploads |
| `GCP_LOCATION` | ❌ | `us-central1` | Vertex AI region |

## License

MIT
