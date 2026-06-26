# Snagger

A small app that accepts a YouTube URL and saves either an MP3 audio file or an MP4 video file.

Use this only for videos you own, videos in the public domain, or videos where you have permission to download and convert the media.

## Quality note

YouTube generally serves audio as Opus or AAC, not MP3. MP3 is a lossy format, so a literally lossless MP3 is not possible when the source is YouTube audio. This app downloads the best available audio stream and converts it with FFmpeg at the highest MP3 VBR setting by default.

For no-extra-loss archiving, enable "Keep original source audio" so the app saves the original YouTube audio file alongside the MP3.

For MP4 output, Snagger asks yt-dlp for H.264/AVC video in an MP4 container, then uses FFmpeg to normalize the final audio stream to AAC-LC stereo at 48 kHz. That avoids YouTube's AV1 (`av01`) MP4 streams and source audio variants that some editors such as Adobe Premiere may reject.

## Run the web app with Docker

On your Linux Mint Debian Edition server, install Docker and the Compose plugin, then run:

```bash
git clone <your-repo-url> snagger
cd snagger
sudo mkdir -p /mnt/tang/media/downloads
sudo chown -R 1000:1000 /mnt/tang/media/downloads
docker compose up -d --build
```

Open:

```text
http://your-server-ip:8090
```

Downloads are saved on the host in:

```text
/mnt/tang/media/downloads
```

Snagger sends files to the browser by default. Check "Deploy to server" in the
web app when you also want the finished MP3 or MP4 saved to that host folder.
When MP3 mode sees a YouTube playlist URL, Snagger shows a playlist option and
downloads each playlist item as a separate MP3. Browser-only playlist jobs are
returned as one zip file; server-deployed playlist jobs also leave the separate
MP3 files in the mounted downloads folder.

To use a different host folder for downloads:

```bash
SNAGGER_DOWNLOADS_DIR=/some/other/downloads docker compose up -d --build
```

If port 8090 is already in use, choose another free host port without changing
the container's internal port:

```bash
SNAGGER_HOST_PORT=8091 docker compose up -d --build
```

To enable browser Basic Auth, set both values:

```bash
SNAGGER_USERNAME=change-me SNAGGER_PASSWORD=change-me docker compose up -d
```

## Deploy with Portainer

For a Portainer stack, use an absolute host path for downloads. On the server:

```bash
sudo mkdir -p /mnt/tang/media/downloads
sudo chown -R 1000:1000 /mnt/tang/media/downloads
```

If you create the stack from a Git repository, use the repository's
`docker-compose.yml` as-is. In the Portainer stack environment variables, set:

```text
SNAGGER_HOST_PORT=8090
SNAGGER_DOWNLOADS_DIR=/mnt/tang/media/downloads
```

If you want Basic Auth, also set:

```text
SNAGGER_USERNAME=your-user
SNAGGER_PASSWORD=your-password
```

If you use Portainer's Web editor, first build the image on the server:

```bash
cd /path/to/snagger
docker build -t snagger:local .
```

Then paste this compose file into the Web editor:

```yaml
services:
  snagger:
    image: snagger:local
    container_name: snagger
    restart: unless-stopped
    ports:
      - "${SNAGGER_HOST_PORT:-8090}:8080"
    environment:
      SNAGGER_OUTPUT_DIR: /downloads
      SNAGGER_USERNAME: ${SNAGGER_USERNAME:-}
      SNAGGER_PASSWORD: ${SNAGGER_PASSWORD:-}
    volumes:
      - ${SNAGGER_DOWNLOADS_DIR:-/mnt/tang/media/downloads}:/downloads
```

Deploy the stack, then open `http://your-server-ip:8090`.

## Run the web app without Docker

Install system FFmpeg first:

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg
```

Create a virtual environment and run the web app:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
SNAGGER_OUTPUT_DIR="$PWD/downloads" snagger-web
```

The web server listens on:

```text
http://0.0.0.0:8080
```

For a more production-like native run, put it behind a reverse proxy and use Gunicorn:

```bash
pip install gunicorn
SNAGGER_OUTPUT_DIR="$PWD/downloads" gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 8 --timeout 900 youtube_audio_extractor.web:app
```

Keep `--workers 1` unless you add a shared job store such as Redis; job state is currently kept in memory.

## Run the Windows desktop app from source

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
snagger
```

If your Windows install uses the Python launcher, `py -3` works too.

## Build the Windows executable

```powershell
.\build_windows.ps1
```

The executable will be created at:

```text
dist\Snagger.exe
```

The build includes FFmpeg through the `imageio-ffmpeg` package, so users should not need a separate FFmpeg install for the packaged desktop app.
