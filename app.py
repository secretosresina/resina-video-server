from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse, Response, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
import subprocess
import os
import glob
import uuid
import threading
import urllib.request
import urllib.error
import urllib.parse
import json
import time
import traceback

app = FastAPI(title="Resina Video Server")

BASE_URL = os.getenv("BASE_URL", "https://resina-video-server.onrender.com").rstrip("/")
VIDEO_DIR = "/tmp/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI", f"{BASE_URL}/tiktok/callback")

TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
TIKTOK_PUBLISH_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TIKTOK_SCOPES = "user.info.basic,video.publish,video.upload"

TIKTOK_TOKEN_FILE = os.path.join(VIDEO_DIR, "tiktok_tokens.json")
OAUTH_STATE_FILE = os.path.join(VIDEO_DIR, "tiktok_oauth_states.json")

TIKTOK_VERIFICATION_FILE = "tiktokMBXNgoJHxI9pXwUdcx90DU4Hgx7rg8RV.txt"
TIKTOK_VERIFICATION_CONTENT = "tiktok-developers-site-verification=MBXNgoJHxI9pXwUdcx90DU4Hgx7rg8RV"


# ============================================================
# JSON / JOB STATE
# ============================================================

def atomic_json_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def job_state_path(job_id):
    return os.path.join(VIDEO_DIR, f"{job_id}.json")


def save_job_state(job_id, data):
    atomic_json_write(job_state_path(job_id), data)


def load_job_state(job_id):
    path = job_state_path(job_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
# TIKTOK TOKEN STORAGE
# ============================================================

def save_tiktok_tokens(token_data):
    data = dict(token_data)
    data["saved_at"] = int(time.time())
    atomic_json_write(TIKTOK_TOKEN_FILE, data)


def load_tiktok_tokens():
    if not os.path.isfile(TIKTOK_TOKEN_FILE):
        return None
    try:
        with open(TIKTOK_TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
# TIKTOK OAUTH STATE
# ============================================================

def save_oauth_state(state):
    states = {}

    if os.path.isfile(OAUTH_STATE_FILE):
        try:
            with open(OAUTH_STATE_FILE, "r", encoding="utf-8") as f:
                states = json.load(f)
        except Exception:
            states = {}

    now = int(time.time())
    states[state] = now

    states = {
        k: v for k, v in states.items()
        if now - int(v) < 600
    }

    atomic_json_write(OAUTH_STATE_FILE, states)


def consume_oauth_state(state):
    if not os.path.isfile(OAUTH_STATE_FILE):
        return False

    try:
        with open(OAUTH_STATE_FILE, "r", encoding="utf-8") as f:
            states = json.load(f)
    except Exception:
        return False

    created_at = states.pop(state, None)

    try:
        atomic_json_write(OAUTH_STATE_FILE, states)
    except Exception:
        pass

    return (
        created_at is not None
        and int(time.time()) - int(created_at) <= 600
    )


# ============================================================
# TIKTOK VERIFICATION
# ============================================================

@app.get(
    f"/{TIKTOK_VERIFICATION_FILE}",
    response_class=PlainTextResponse
)
def tiktok_verification():
    return TIKTOK_VERIFICATION_CONTENT


# ============================================================
# TIKTOK LOGIN
# ============================================================

@app.get("/tiktok/login")
def tiktok_login():
    if not TIKTOK_CLIENT_KEY:
        raise HTTPException(
            500,
            "Falta TIKTOK_CLIENT_KEY en Render."
        )

    if not TIKTOK_CLIENT_SECRET:
        raise HTTPException(
            500,
            "Falta TIKTOK_CLIENT_SECRET en Render."
        )

    state = uuid.uuid4().hex
    save_oauth_state(state)

    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": TIKTOK_SCOPES,
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
    }

    return RedirectResponse(
        TIKTOK_AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(params)
    )


# ============================================================
# TIKTOK CALLBACK
# ============================================================

@app.get("/tiktok/callback")
def tiktok_callback(
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None
):
    if error:
        return {
            "success": False,
            "stage": "tiktok_callback",
            "error": error,
            "error_description": error_description
        }

    if not code:
        return {
            "success": False,
            "stage": "tiktok_callback",
            "error": "TikTok no devolvió authorization code."
        }

    if not state:
        return {
            "success": False,
            "stage": "tiktok_callback",
            "error": "TikTok no devolvió state."
        }

    if not consume_oauth_state(state):
        return {
            "success": False,
            "stage": "tiktok_callback",
            "error": "State inválido o expirado. Vuelve a iniciar sesión."
        }

    try:
        token_data = exchange_code_for_token(code)
        save_tiktok_tokens(token_data)

        user_data = get_tiktok_user(
            token_data["access_token"]
        )

        return {
            "success": True,
            "message": "TikTok conectado correctamente.",
            "open_id": token_data.get("open_id"),
            "scope": token_data.get("scope"),
            "user": user_data
        }

    except Exception as e:
        return {
            "success": False,
            "stage": "token_exchange",
            "error": str(e)
        }


# ============================================================
# INTERCAMBIAR CODE POR TOKEN
# ============================================================

def exchange_code_for_token(code):
    form_data = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TIKTOK_REDIRECT_URI
    }

    body = urllib.parse.urlencode(
        form_data
    ).encode("utf-8")

    request = urllib.request.Request(
        TIKTOK_TOKEN_URL,
        data=body,
        method="POST"
    )

    request.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded"
    )

    request.add_header(
        "Cache-Control",
        "no-cache"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            data = json.loads(
                response.read().decode("utf-8")
            )

        if "access_token" not in data:
            raise Exception(
                f"TikTok no devolvió access_token: {data}"
            )

        return data

    except urllib.error.HTTPError as e:
        raise Exception(
            f"TikTok OAuth HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')}"
        )


# ============================================================
# REFRESH TOKEN TIKTOK
# ============================================================

def refresh_tiktok_token():
    tokens = load_tiktok_tokens()

    if not tokens:
        raise Exception(
            "No existe una sesión TikTok. Abre /tiktok/login primero."
        )

    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        raise Exception(
            "No existe refresh_token."
        )

    form_data = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    body = urllib.parse.urlencode(
        form_data
    ).encode("utf-8")

    request = urllib.request.Request(
        TIKTOK_TOKEN_URL,
        data=body,
        method="POST"
    )

    request.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            new_tokens = json.loads(
                response.read().decode("utf-8")
            )

        if "access_token" not in new_tokens:
            raise Exception(
                f"TikTok no devolvió un nuevo access_token: {new_tokens}"
            )

        save_tiktok_tokens(new_tokens)

        return new_tokens

    except urllib.error.HTTPError as e:
        raise Exception(
            f"Error refrescando token TikTok HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')}"
        )


# ============================================================
# TOKEN ACTUAL
# ============================================================

def get_tiktok_access_token():
    tokens = load_tiktok_tokens()

    if not tokens:
        raise Exception(
            "TikTok no está conectado. Abre /tiktok/login."
        )

    access_token = tokens.get("access_token")

    if not access_token:
        raise Exception(
            "No existe access_token."
        )

    saved_at = int(
        tokens.get("saved_at", 0)
    )

    expires_in = int(
        tokens.get("expires_in", 86400)
    )

    if time.time() >= (
        saved_at
        + expires_in
        - 600
    ):

        try:
            refreshed = refresh_tiktok_token()
            return refreshed["access_token"]

        except Exception:
            return access_token

    return access_token


# ============================================================
# TIKTOK JSON REQUEST
# ============================================================

def tiktok_json_request(
    url,
    method="POST",
    payload=None
):
    access_token = get_tiktok_access_token()

    body = None

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method=method
    )

    request.add_header(
        "Authorization",
        f"Bearer {access_token}"
    )

    request.add_header(
        "Content-Type",
        "application/json; charset=UTF-8"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:

            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as e:
        raise Exception(
            f"TikTok API HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')}"
        )


# ============================================================
# TIKTOK USER
# ============================================================

def get_tiktok_user(access_token):
    params = urllib.parse.urlencode({
        "fields":
        "open_id,display_name,avatar_url,profile_deep_link"
    })

    url = (
        TIKTOK_USER_INFO_URL
        + "?"
        + params
    )

    request = urllib.request.Request(
        url,
        method="GET"
    )

    request.add_header(
        "Authorization",
        f"Bearer {access_token}"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as e:
        raise Exception(
            f"TikTok user info error {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')}"
        )


@app.get("/tiktok/me")
def tiktok_me():
    try:
        access_token = get_tiktok_access_token()
        data = get_tiktok_user(access_token)

        return {
            "success": True,
            "connected": True,
            "data": data
        }

    except Exception as e:
        return {
            "success": False,
            "connected": False,
            "error": str(e)
        }


@app.get("/tiktok/creator-info")
def tiktok_creator_info():
    try:
        data = tiktok_json_request(
            TIKTOK_CREATOR_INFO_URL,
            method="POST",
            payload={}
        )

        return {
            "success": True,
            "data": data
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# TIKTOK PUBLISH
# ============================================================

class TikTokPublishRequest(BaseModel):
    video_url: str
    title: str = ""
    privacy_level: str = "SELF_ONLY"
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False
    is_aigc: bool = False


@app.post("/tiktok/publish")
def tiktok_publish(
    data: TikTokPublishRequest
):
    if not data.video_url.startswith("https://"):
        raise HTTPException(
            400,
            "video_url debe comenzar con https://"
        )

    if not data.video_url.startswith(BASE_URL):
        raise HTTPException(
            400,
            f"Por seguridad, el video debe estar alojado en {BASE_URL}"
        )

    allowed_privacy = {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY"
    }

    if data.privacy_level not in allowed_privacy:
        raise HTTPException(
            400,
            "privacy_level inválido."
        )

    post_info = {
        "title": data.title[:2200],
        "privacy_level": data.privacy_level,
        "disable_comment": data.disable_comment,
        "disable_duet": data.disable_duet,
        "disable_stitch": data.disable_stitch
    }

    if data.is_aigc:
        post_info["is_aigc"] = True

    payload = {
        "post_info": post_info,
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": data.video_url
        }
    }

    try:
        result = tiktok_json_request(
            TIKTOK_PUBLISH_URL,
            method="POST",
            payload=payload
        )

        error_data = result.get(
            "error",
            {}
        )

        if (
            error_data
            and error_data.get("code")
            not in (None, "", "ok")
        ):
            return {
                "success": False,
                "tiktok": result
            }

        return {
            "success": True,
            "publish_id":
                result.get(
                    "data",
                    {}
                ).get(
                    "publish_id"
                ),
            "tiktok": result
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


class TikTokStatusRequest(BaseModel):
    publish_id: str


@app.post("/tiktok/status")
def tiktok_status(
    data: TikTokStatusRequest
):
    try:
        result = tiktok_json_request(
            TIKTOK_STATUS_URL,
            method="POST",
            payload={
                "publish_id":
                    data.publish_id
            }
        )

        return {
            "success": True,
            "data": result
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# HOME / TESTS
# ============================================================

@app.get("/")
def home():
    return {
        "status": "online",
        "message":
            "Servidor de procesamiento de videos funcionando",
        "tiktok_oauth":
            f"{BASE_URL}/tiktok/login",
        "tiktok_test":
            f"{BASE_URL}/tiktok/me"
    }


@app.get("/test")
def test():
    return {
        "success": True,
        "message":
            "Render está conectado correctamente"
    }


@app.get("/test_ffmpeg")
def test_ffmpeg():
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10
        )

        return {
            "success": True,
            "ffmpeg":
                result.stdout.splitlines()[0]
                if result.stdout
                else "FFmpeg encontrado"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# DESCARGAR VIDEO TIKTOK
# ============================================================

class VideoRequest(BaseModel):
    tiktok_url: str


@app.post("/download_video")
def download_video(
    data: VideoRequest
):
    if not data.tiktok_url.startswith(
        ("http://", "https://")
    ):
        raise HTTPException(
            400,
            "URL no válida"
        )

    job_id = str(uuid.uuid4())

    output_template = os.path.join(
        VIDEO_DIR,
        f"{job_id}.%(ext)s"
    )

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                output_template,
                data.tiktok_url
            ],
            capture_output=True,
            text=True,
            timeout=180
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error":
                    result.stderr[-3000:]
            }

        files = glob.glob(
            os.path.join(
                VIDEO_DIR,
                f"{job_id}.*"
            )
        )

        if not files:
            return {
                "success": False,
                "error":
                    "El video no fue encontrado después de la descarga"
            }

        filename = os.path.basename(
            files[0]
        )

        return {
            "success": True,
            "job_id":
                job_id,
            "filename":
                filename,
            "download_url":
                f"{BASE_URL}/video/{filename}"
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error":
                "La descarga superó 180 segundos"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# SERVIR VIDEOS
# ============================================================

def get_video_path(filename):
    safe_filename = os.path.basename(
        filename
    )

    return os.path.join(
        VIDEO_DIR,
        safe_filename
    )


@app.head("/video/{filename}")
def head_video(filename: str):
    filepath = get_video_path(filename)

    if not os.path.isfile(filepath):
        raise HTTPException(
            404,
            "Video no encontrado"
        )

    file_size = os.path.getsize(
        filepath
    )

    return Response(
        status_code=200,
        headers={
            "Content-Type":
                "video/mp4",
            "Content-Length":
                str(file_size),
            "Accept-Ranges":
                "bytes",
            "Content-Disposition":
                "inline",
            "Cache-Control":
                "public, max-age=3600"
        }
    )


@app.get("/video/{filename}")
def get_video(
    filename: str,
    request: Request
):
    filepath = get_video_path(filename)

    if not os.path.isfile(filepath):
        raise HTTPException(
            404,
            "Video no encontrado"
        )

    file_size = os.path.getsize(filepath)

    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            filepath,
            media_type="video/mp4",
            headers={
                "Content-Length":
                    str(file_size),
                "Accept-Ranges":
                    "bytes",
                "Content-Disposition":
                    "inline",
                "Cache-Control":
                    "public, max-age=3600"
            }
        )

    try:
        range_value = (
            range_header
            .replace("bytes=", "", 1)
            .strip()
        )

        if "," in range_value:
            raise ValueError(
                "Multiple ranges no soportados"
            )

        start_str, end_str = (
            range_value.split("-", 1)
        )

        if start_str:
            start = int(start_str)
        else:
            suffix_length = int(end_str)

            if suffix_length <= 0:
                raise ValueError(
                    "Range inválido"
                )

            start = max(
                file_size - suffix_length,
                0
            )

        if end_str:
            end = int(end_str)
        else:
            end = file_size - 1

        start = max(start, 0)
        end = min(end, file_size - 1)

        if (
            start > end
            or start >= file_size
        ):
            return Response(
                status_code=416,
                headers={
                    "Content-Range":
                        f"bytes */{file_size}"
                }
            )

        content_length = (
            end - start + 1
        )

        def iter_file():
            with open(
                filepath,
                "rb"
            ) as video_file:

                video_file.seek(start)

                remaining = content_length

                while remaining > 0:
                    chunk = video_file.read(
                        min(
                            1024 * 1024,
                            remaining
                        )
                    )

                    if not chunk:
                        break

                    remaining -= len(chunk)

                    yield chunk

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Length":
                    str(content_length),
                "Content-Range":
                    f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges":
                    "bytes",
                "Content-Disposition":
                    "inline",
                "Cache-Control":
                    "public, max-age=3600"
            }
        )

    except Exception:
        return Response(
            status_code=416,
            headers={
                "Content-Range":
                    f"bytes */{file_size}"
            }
        )


# ============================================================
# ELEVENLABS MULTIPART
# ============================================================

def create_multipart_body(
    file_data,
    filename,
    text
):
    """
    Construye multipart/form-data sin f-strings rotos.
    Esta era la parte que estaba provocando el SyntaxError.
    """

    boundary = (
        "----ResinaBoundary"
        + uuid.uuid4().hex
    )

    body = bytearray()

    body.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; '
            f'name="file"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: audio/mpeg\r\n"
            f"\r\n"
        ).encode("utf-8")
    )

    body.extend(file_data)
    body.extend(b"\r\n")

    body.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; '
            f'name="text"\r\n'
            f"\r\n"
        ).encode("utf-8")
    )

    body.extend(
        text.encode("utf-8")
    )

    body.extend(b"\r\n")

    body.extend(
        (
            f"--{boundary}--\r\n"
        ).encode("utf-8")
    )

    return bytes(body), boundary


# ============================================================
# ELEVENLABS FORCED ALIGNMENT
# ============================================================

def get_forced_alignment(
    audio_path,
    text
):
    if not ELEVENLABS_API_KEY:
        raise Exception(
            "Falta ELEVENLABS_API_KEY en Render"
        )

    with open(
        audio_path,
        "rb"
    ) as f:
        audio_data = f.read()

    body, boundary = create_multipart_body(
        audio_data,
        os.path.basename(audio_path),
        text
    )

    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/forced-alignment",
        data=body,
        method="POST"
    )

    request.add_header(
        "xi-api-key",
        ELEVENLABS_API_KEY
    )

    request.add_header(
        "Content-Type",
        f"multipart/form-data; boundary={boundary}"
    )

    request.add_header(
        "Accept",
        "application/json"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=120
        ) as response:

            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as e:
        raise Exception(
            "ElevenLabs Forced Alignment "
            f"error {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')[-3000:]}"
        )

    except Exception as e:
        raise Exception(
            "No se pudo obtener la sincronización "
            f"de ElevenLabs: {e}"
        )


# ============================================================
# SRT
# ============================================================

def format_srt_time(seconds):
    milliseconds = int(
        round(
            (seconds % 1) * 1000
        )
    )

    total_seconds = int(seconds)

    if milliseconds >= 1000:
        milliseconds = 0
        total_seconds += 1

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{milliseconds:03d}"
    )


def create_srt_from_alignment(
    alignment,
    srt_path
):
    words = alignment.get(
        "words",
        []
    )

    if not words:
        raise Exception(
            "ElevenLabs no devolvió palabras para sincronizar"
        )

    subtitles = []
    current_words = []
    current_start = None
    current_end = None
    current_chars = 0
    previous_end = None

    MAX_WORDS = 7
    MAX_CHARS = 42

    for word_data in words:
        word = str(
            word_data.get(
                "text",
                ""
            )
        ).strip()

        if not word:
            continue

        start = float(
            word_data.get(
                "start",
                0
            )
        )

        end = float(
            word_data.get(
                "end",
                start
            )
        )

        projected_chars = (
            current_chars
            + len(word)
            + (
                1
                if current_words
                else 0
            )
        )

        large_pause = (
            previous_end is not None
            and start - previous_end >= 0.45
        )

        should_break = (
            current_words
            and (
                len(current_words) >= MAX_WORDS
                or projected_chars > MAX_CHARS
                or large_pause
            )
        )

        if should_break:
            subtitles.append(
                (
                    current_start,
                    current_end,
                    " ".join(current_words)
                )
            )

            current_words = []
            current_start = None
            current_end = None
            current_chars = 0

        if current_start is None:
            current_start = start

        current_words.append(word)
        current_end = end

        current_chars += (
            len(word)
            + (
                1
                if len(current_words) > 1
                else 0
            )
        )

        previous_end = end

    if current_words:
        subtitles.append(
            (
                current_start,
                current_end,
                " ".join(current_words)
            )
        )

    with open(
        srt_path,
        "w",
        encoding="utf-8"
    ) as f:

        for index, (
            start,
            end,
            subtitle_text
        ) in enumerate(
            subtitles,
            start=1
        ):

            f.write(
                f"{index}\n"
            )

            f.write(
                f"{format_srt_time(start)} --> "
                f"{format_srt_time(end)}\n"
            )

            f.write(
                f"{subtitle_text}\n\n"
            )


# ============================================================
# DURACIONES
# ============================================================

def get_audio_duration(audio_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path
        ],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise Exception(
            "No se pudo obtener la duración del audio: "
            + result.stderr[-2000:]
        )

    return float(
        result.stdout.strip()
    )


def get_video_duration(video_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path
        ],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise Exception(
            "No se pudo obtener la duración del video: "
            + result.stderr[-2000:]
        )

    return float(
        result.stdout.strip()
    )


# ============================================================
# VALIDAR MP4
# ============================================================

def validate_output_video(video_path):
    if not os.path.isfile(video_path):
        raise Exception(
            "El archivo MP4 no existe"
        )

    file_size = os.path.getsize(
        video_path
    )

    if file_size < 50 * 1024:
        raise Exception(
            "El MP4 parece incompleto: "
            f"tamaño de {file_size} bytes"
        )

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            video_path
        ],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise Exception(
            "ffprobe no pudo leer el MP4: "
            + result.stderr[-3000:]
        )

    try:
        probe = json.loads(
            result.stdout
        )
    except json.JSONDecodeError as e:
        raise Exception(
            "ffprobe devolvió una respuesta inválida: "
            + str(e)
        )

    format_info = probe.get("format") or {}
    format_name = format_info.get("format_name") or ""

    try:
        duration = float(
            format_info.get("duration")
        )
    except (TypeError, ValueError):
        duration = 0

    allowed_formats = {
        "mov",
        "mp4",
        "m4a",
        "3gp",
        "3g2",
        "mj2"
    }

    format_ok = any(
        name in allowed_formats
        for name in format_name.split(",")
    )

    streams = probe.get("streams") or []

    video_codecs = {
        stream.get("codec_name")
        for stream in streams
        if stream.get("codec_type") == "video"
    }

    audio_codecs = {
        stream.get("codec_name")
        for stream in streams
        if stream.get("codec_type") == "audio"
    }

    if not format_ok:
        raise Exception(
            "El archivo no es un contenedor MP4/MOV válido. "
            f"format_name={format_name}"
        )

    if duration <= 0:
        raise Exception(
            "El MP4 tiene una duración inválida"
        )

    if "h264" not in video_codecs:
        raise Exception(
            "El MP4 no contiene video H.264. "
            f"Codecs encontrados: {sorted(video_codecs)}"
        )

    if "aac" not in audio_codecs:
        raise Exception(
            "El MP4 no contiene audio AAC. "
            f"Codecs encontrados: {sorted(audio_codecs)}"
        )

    return {
        "valid": True,
        "size": file_size,
        "duration": duration,
        "format": format_name,
        "video_codecs":
            sorted(video_codecs),
        "audio_codecs":
            sorted(audio_codecs)
    }


# ============================================================
# LIMPIEZA
# ============================================================

def cleanup_old_video_files(
    max_age_hours=24
):
    now = time.time()
    max_age = max_age_hours * 3600

    try:
        for filepath in glob.glob(
            os.path.join(VIDEO_DIR, "*")
        ):
            if not os.path.isfile(filepath):
                continue

            try:
                age = (
                    now
                    - os.path.getmtime(filepath)
                )

                if age > max_age:
                    os.remove(filepath)

            except Exception:
                continue

    except Exception:
        pass


cleanup_old_video_files()


# ============================================================
# FFMPEG
# ============================================================

def run_ffmpeg(
    job_id,
    input_path,
    output_path,
    voice_path,
    background_path,
    srt_path,
    duration
):
    temp_output_path = (
        output_path
        + ".tmp.mp4"
    )

    if os.path.isfile(
        temp_output_path
    ):
        os.remove(
            temp_output_path
        )

    subtitle_filter_path = (
        srt_path
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )

    filter_complex = (
        "[1:a]"
        "volume=1.0"
        "[voice];"

        "[2:a]"
        "volume=0.20"
        "[bg];"

        "[voice][bg]"
        "amix=inputs=2:"
        "duration=first:"
        "dropout_transition=2"
        "[audio];"

        "[0:v]"
        "subtitles='"
        + subtitle_filter_path
        + "':"
        "force_style="
        "'FontName=Arial,"
        "FontSize=18,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        "Outline=3,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginV=25'"
        "[video]"
    )

    save_job_state(
        job_id,
        {
            **(
                load_job_state(job_id)
                or {}
            ),
            "status":
                "processing",
            "stage":
                "ffmpeg",
            "ffmpeg_started_at":
                int(time.time())
        }
    )

    print(
        f"[JOB {job_id}] FFmpeg START",
        flush=True
    )

    command = [
        "ffmpeg",
        "-y",

        "-stream_loop",
        "-1",
        "-i",
        input_path,

        "-i",
        voice_path,

        "-stream_loop",
        "-1",
        "-i",
        background_path,

        "-filter_complex",
        filter_complex,

        "-map",
        "[video]",

        "-map",
        "[audio]",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "23",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-t",
        str(duration),

        "-movflags",
        "+faststart",

        temp_output_path
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300
    )

    print(
        f"[JOB {job_id}] FFmpeg END "
        f"returncode={result.returncode}",
        flush=True
    )

    if result.returncode != 0:
        if os.path.isfile(temp_output_path):
            try:
                os.remove(temp_output_path)
            except Exception:
                pass

        raise Exception(
            "FFmpeg error:\n"
            + result.stderr[-6000:]
        )

    validation = validate_output_video(
        temp_output_path
    )

    os.replace(
        temp_output_path,
        output_path
    )

    return validation


# ============================================================
# PROCESAMIENTO BACKGROUND
# ============================================================

def process_video_background(
    job_id,
    input_path,
    output_path,
    voice_path,
    background_path,
    subtitle_text
):
    state = (
        load_job_state(job_id)
        or {}
    )

    try:
        state.update({
            "status":
                "processing",
            "stage":
                "alignment"
        })

        save_job_state(
            job_id,
            state
        )

        print(
            f"[JOB {job_id}] "
            "Alignment START",
            flush=True
        )

        alignment = get_forced_alignment(
            voice_path,
            subtitle_text
        )

        srt_path = os.path.join(
            VIDEO_DIR,
            f"{job_id}.srt"
        )

        create_srt_from_alignment(
            alignment,
            srt_path
        )

        voice_duration = (
            get_audio_duration(
                voice_path
            )
        )

        video_duration = (
            get_video_duration(
                input_path
            )
        )

        state.update({
            "voice_duration":
                voice_duration,
            "video_duration":
                video_duration,
            "stage":
                "ffmpeg"
        })

        save_job_state(
            job_id,
            state
        )

        print(
            f"[JOB {job_id}] "
            f"Alignment END "
            f"voice={voice_duration:.2f}s "
            f"video={video_duration:.2f}s",
            flush=True
        )

        validation = run_ffmpeg(
            job_id,
            input_path,
            output_path,
            voice_path,
            background_path,
            srt_path,
            voice_duration
        )

        filename = os.path.basename(
            output_path
        )

        state.update({
            "status":
                "completed",
            "stage":
                "completed",
            "filename":
                filename,
            "download_url":
                f"{BASE_URL}/video/{filename}",
            "alignment":
                True,
            "output_size":
                validation["size"],
            "output_duration":
                validation["duration"],
            "output_format":
                validation["format"],
            "output_video_codec":
                validation["video_codecs"],
            "output_audio_codec":
                validation["audio_codecs"],
            "completed_at":
                int(time.time())
        })

        save_job_state(
            job_id,
            state
        )

        print(
            f"[JOB {job_id}] "
            f"COMPLETED "
            f"{state['download_url']}",
            flush=True
        )

        cleanup_old_video_files()

    except subprocess.TimeoutExpired:
        state.update({
            "status":
                "error",
            "stage":
                "ffmpeg",
            "error":
                "FFmpeg superó 300 segundos"
        })

        save_job_state(
            job_id,
            state
        )

        print(
            f"[JOB {job_id}] TIMEOUT",
            flush=True
        )

    except Exception as e:
        state.update({
            "status":
                "error",
            "stage":
                state.get(
                    "stage",
                    "processing"
                ),
            "error":
                str(e),
            "traceback":
                traceback.format_exc()[-6000:]
        })

        save_job_state(
            job_id,
            state
        )

        print(
            f"[JOB {job_id}] ERROR {e}",
            flush=True
        )


# ============================================================
# PROCESS VIDEO
# ============================================================

@app.post("/process_video")
async def process_video(
    video: UploadFile = File(...),
    voice: UploadFile = File(...),
    background: str = Form(...),
    text: str = Form(...)
):
    job_id = str(
        uuid.uuid4()
    )

    input_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_input.mp4"
    )

    voice_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_voice.mp3"
    )

    background_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_background.mp3"
    )

    output_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_processed.mp4"
    )

    try:
        with open(
            input_path,
            "wb"
        ) as f:
            while True:
                chunk = await video.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                f.write(chunk)

        with open(
            voice_path,
            "wb"
        ) as f:
            while True:
                chunk = await voice.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                f.write(chunk)

        urllib.request.urlretrieve(
            background,
            background_path
        )

        if not os.path.isfile(
            background_path
        ):
            raise Exception(
                "No se pudo descargar "
                "el sonido de fondo"
            )

        if not text.strip():
            raise Exception(
                "El texto de subtítulos "
                "está vacío"
            )

        if not os.path.isfile(
            voice_path
        ):
            raise Exception(
                "No se pudo guardar "
                "el audio de ElevenLabs"
            )

        save_job_state(
            job_id,
            {
                "status":
                    "queued",
                "stage":
                    "queued",
                "created_at":
                    int(time.time())
            }
        )

        thread = threading.Thread(
            target=process_video_background,
            args=(
                job_id,
                input_path,
                output_path,
                voice_path,
                background_path,
                text
            ),
            daemon=True
        )

        thread.start()

        return {
            "success":
                True,
            "job_id":
                job_id,
            "status":
                "processing",
            "stage":
                "queued"
        }

    except Exception as e:
        save_job_state(
            job_id,
            {
                "status":
                    "error",
                "stage":
                    "upload",
                "error":
                    str(e)
            }
        )

        return {
            "success":
                False,
            "job_id":
                job_id,
            "status":
                "error",
            "error":
                str(e)
        }


# ============================================================
# STATUS VIDEO
# ============================================================

@app.get("/status/{job_id}")
def get_status(
    job_id: str
):
    output_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_processed.mp4"
    )

    state = load_job_state(
        job_id
    )

    if os.path.isfile(
        output_path
    ):
        try:
            validation = validate_output_video(
                output_path
            )

            filename = os.path.basename(
                output_path
            )

            recovered = {
                "status":
                    "completed",
                "stage":
                    "completed",
                "filename":
                    filename,
                "download_url":
                    f"{BASE_URL}/video/{filename}",
                "output_size":
                    validation["size"],
                "output_duration":
                    validation["duration"],
                "output_format":
                    validation["format"],
                "output_video_codec":
                    validation["video_codecs"],
                "output_audio_codec":
                    validation["audio_codecs"]
            }

            if state:
                for key in [
                    "voice_duration",
                    "video_duration",
                    "alignment"
                ]:
                    if key in state:
                        recovered[key] = state[key]

            return {
                "success":
                    True,
                "job_id":
                    job_id,
                **recovered
            }

        except Exception as e:
            if state is None:
                state = {}

            state.update({
                "status":
                    "error",
                "stage":
                    "validation",
                "error":
                    str(e)
            })

            save_job_state(
                job_id,
                state
            )

    if state:
        return {
            "success":
                True,
            "job_id":
                job_id,
            **state
        }

    raise HTTPException(
        404,
        "Job no encontrado"
    )


# ============================================================
# DIAGNÓSTICO
# ============================================================

@app.get("/debug/{job_id}")
def debug_job(
    job_id: str
):
    state = load_job_state(
        job_id
    )

    files = []

    for path in glob.glob(
        os.path.join(
            VIDEO_DIR,
            f"{job_id}*"
        )
    ):
        try:
            files.append({
                "file":
                    os.path.basename(path),
                "size":
                    os.path.getsize(path),
                "age_seconds":
                    round(
                        time.time()
                        - os.path.getmtime(path),
                        1
                    )
            })
        except Exception:
            pass

    return {
        "success":
            True,
        "job_id":
            job_id,
        "state":
            state,
        "files":
            files,
        "pid":
            os.getpid(),
        "time":
            int(time.time())
    }


@app.get("/health")
def health():
    return {
        "success":
            True,
        "status":
            "healthy",
        "pid":
            os.getpid(),
        "time":
            int(time.time())
    }
