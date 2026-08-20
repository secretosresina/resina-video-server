from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import os
import glob
import uuid
import threading
import urllib.request
import urllib.error
import json


app = FastAPI()

VIDEO_DIR = "/tmp/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")


class VideoRequest(BaseModel):
    tiktok_url: str


# ============================================================
# UTILIDADES DE ESTADO
# ============================================================

def job_state_path(job_id):
    return os.path.join(
        VIDEO_DIR,
        f"{job_id}.json"
    )


def save_job_state(job_id, data):

    path = job_state_path(job_id)

    temp_path = path + ".tmp"

    with open(
        temp_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False
        )

    os.replace(
        temp_path,
        path
    )


def load_job_state(job_id):

    path = job_state_path(job_id)

    if not os.path.isfile(path):
        return None

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return None


# ============================================================
# HOME / TEST
# ============================================================

@app.get("/")
def home():

    return {
        "status": "online",
        "message": "Servidor de procesamiento de videos funcionando"
    }


@app.get("/test")
def test():

    return {
        "success": True,
        "message": "Render está conectado correctamente"
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

        version = (
            result.stdout.splitlines()[0]
            if result.stdout
            else "FFmpeg encontrado"
        )

        return {
            "success": True,
            "ffmpeg": version
        }

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# DESCARGAR VIDEO DE TIKTOK
# ============================================================

@app.post("/download_video")
def download_video(data: VideoRequest):

    if not data.tiktok_url.startswith(
        ("http://", "https://")
    ):

        raise HTTPException(
            status_code=400,
            detail="URL no válida"
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
                "-f", "bv*+ba/b",
                "--merge-output-format", "mp4",
                "-o", output_template,
                data.tiktok_url
            ],
            capture_output=True,
            text=True,
            timeout=180
        )

        if result.returncode != 0:

            return {
                "success": False,
                "error": result.stderr[-3000:]
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
                "error": (
                    "El video no fue encontrado "
                    "después de la descarga"
                )
            }

        filename = os.path.basename(
            files[0]
        )

        return {
            "success": True,
            "job_id": job_id,
            "filename": filename,
            "download_url": (
                "https://resina-video-server.onrender.com"
                f"/video/{filename}"
            )
        }

    except subprocess.TimeoutExpired:

        return {
            "success": False,
            "error": "La descarga superó 180 segundos"
        }

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# SERVIR VIDEOS
# ============================================================

@app.get("/video/{filename}")
def get_video(filename: str):

    filepath = os.path.join(
        VIDEO_DIR,
        filename
    )

    if not os.path.isfile(filepath):

        raise HTTPException(
            status_code=404,
            detail="Video no encontrado"
        )

    return FileResponse(
        filepath,
        media_type="video/mp4",
        filename=filename
    )


# ============================================================
# MULTIPART PARA ELEVENLABS
# ============================================================

def create_multipart_body(
    file_data,
    filename,
    text
):

    boundary = (
        "----WebKitFormBoundary"
        + uuid.uuid4().hex
    )

    body = bytearray()

    body.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; '
            f'name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/mpeg\r\n"
            f"\r\n"
        ).encode("utf-8")
    )

    body.extend(file_data)
    body.extend(b"\r\n")

    body.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="text"\r\n'
            f"\r\n"
        ).encode("utf-8")
    )

    body.extend(
        text.encode("utf-8")
    )

    body.extend(b"\r\n")

    body.extend(
        f"--{boundary}--\r\n".encode(
            "utf-8"
        )
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
            "Falta ELEVENLABS_API_KEY "
            "en Render"
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

    url = (
        "https://api.elevenlabs.io/v1/forced-alignment"
    )

    request = urllib.request.Request(
        url,
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

            response_data = response.read()

        return json.loads(
            response_data.decode("utf-8")
        )

    except urllib.error.HTTPError as e:

        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        raise Exception(
            "ElevenLabs Forced Alignment "
            f"error {e.code}: "
            f"{error_body[-3000:]}"
        )

    except Exception as e:

        raise Exception(
            "No se pudo obtener la "
            f"sincronización de ElevenLabs: {e}"
        )


# ============================================================
# TIEMPOS SRT
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

    minutes = (
        total_seconds % 3600
    ) // 60

    secs = total_seconds % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{milliseconds:03d}"
    )


# ============================================================
# CREAR SUBTÍTULOS SINCRONIZADOS
# ============================================================

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
            "ElevenLabs no devolvió "
            "palabras para sincronizar"
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
            and (
                start - previous_end
                >= 0.45
            )
        )

        should_break = (
            current_words
            and (
                len(current_words)
                >= MAX_WORDS
                or projected_chars
                > MAX_CHARS
                or large_pause
            )
        )

        if should_break:

            subtitles.append(
                (
                    current_start,
                    current_end,
                    " ".join(
                        current_words
                    )
                )
            )

            current_words = []
            current_start = None
            current_end = None
            current_chars = 0

        if current_start is None:

            current_start = start

        current_words.append(
            word
        )

        current_end = end

        current_chars = (
            current_chars
            + len(word)
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
                " ".join(
                    current_words
                )
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
# DURACIÓN DE AUDIO
# ============================================================

def get_audio_duration(
    audio_path
):

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
            "No se pudo obtener la duración "
            "del audio: "
            + result.stderr[-2000:]
        )

    return float(
        result.stdout.strip()
    )


# ============================================================
# DURACIÓN DE VIDEO
# ============================================================

def get_video_duration(
    video_path
):

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
            "No se pudo obtener la duración "
            "del video: "
            + result.stderr[-2000:]
        )

    return float(
        result.stdout.strip()
    )


# ============================================================
# PROCESAMIENTO EN SEGUNDO PLANO
# ============================================================

def process_video_background(
    job_id,
    input_path,
    output_path,
    voice_path,
    background_path,
    subtitle_text
):

    state = load_job_state(
        job_id
    ) or {}

    try:

        state["status"] = "processing"

        save_job_state(
            job_id,
            state
        )

        # ----------------------------------------------------
        # 1. Obtener sincronización real
        # ----------------------------------------------------

        alignment = get_forced_alignment(
            voice_path,
            subtitle_text
        )

        # ----------------------------------------------------
        # 2. Crear SRT
        # ----------------------------------------------------

        srt_path = os.path.join(
            VIDEO_DIR,
            f"{job_id}.srt"
        )

        create_srt_from_alignment(
            alignment,
            srt_path
        )

        # ----------------------------------------------------
        # 3. Duraciones
        # ----------------------------------------------------

        voice_duration = get_audio_duration(
            voice_path
        )

        video_duration = get_video_duration(
            input_path
        )

        state["voice_duration"] = (
            voice_duration
        )

        state["video_duration"] = (
            video_duration
        )

        save_job_state(
            job_id,
            state
        )

        # ----------------------------------------------------
        # 4. Ruta del SRT para FFmpeg
        # ----------------------------------------------------

        subtitle_filter_path = (
            srt_path
            .replace("\\", "/")
            .replace(":", "\\:")
            .replace("'", "\\'")
        )

        # ----------------------------------------------------
        # 5. FFmpeg
        #
        # -stream_loop -1 hace que el video se repita
        # automáticamente.
        #
        # -t limita el resultado a la duración de la voz.
        #
        # Por tanto:
        #
        # video 8s + voz 15s = resultado 15s
        # video 30s + voz 15s = resultado 15s
        # ----------------------------------------------------

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

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",

                # Video
                "-stream_loop",
                "-1",

                "-i",
                input_path,

                # Voz
                "-i",
                voice_path,

                # Música
                "-stream_loop",
                "-1",

                "-i",
                background_path,

                # Filtros
                "-filter_complex",
                filter_complex,

                # Video final
                "-map",
                "[video]",

                # Audio final
                "-map",
                "[audio]",

                # Video
                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "23",

                # Audio
                "-c:a",
                "aac",

                "-b:a",
                "128k",

                # Duración = voz
                "-t",
                str(voice_duration),

                "-movflags",
                "+faststart",

                output_path
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:

            error_message = (
                "FFmpeg error:\n"
                + result.stderr[-5000:]
            )

            state["status"] = "error"
            state["error"] = error_message

            save_job_state(
                job_id,
                state
            )

            return

        if not os.path.isfile(
            output_path
        ):

            state["status"] = "error"
            state["error"] = (
                "FFmpeg no generó "
                "el video"
            )

            save_job_state(
                job_id,
                state
            )

            return

        # ----------------------------------------------------
        # 6. Completado
        # ----------------------------------------------------

        filename = os.path.basename(
            output_path
        )

        state["status"] = "completed"

        state["filename"] = filename

        state["download_url"] = (
            "https://resina-video-server.onrender.com"
            f"/video/{filename}"
        )

        state["alignment"] = True

        save_job_state(
            job_id,
            state
        )

    except subprocess.TimeoutExpired:

        state["status"] = "error"

        state["error"] = (
            "FFmpeg superó "
            "los 300 segundos"
        )

        save_job_state(
            job_id,
            state
        )

    except Exception as e:

        state["status"] = "error"

        state["error"] = str(e)

        save_job_state(
            job_id,
            state
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

        # ----------------------------------------------------
        # Guardar video
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Guardar voz
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Descargar música
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Crear estado persistente
        # ----------------------------------------------------

        save_job_state(
            job_id,
            {
                "status": "queued"
            }
        )

        # ----------------------------------------------------
        # Lanzar procesamiento
        # ----------------------------------------------------

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
            "success": True,
            "job_id": job_id,
            "status": "processing"
        }

    except Exception as e:

        save_job_state(
            job_id,
            {
                "status": "error",
                "error": str(e)
            }
        )

        return {
            "success": False,
            "job_id": job_id,
            "status": "error",
            "error": str(e)
        }


# ============================================================
# STATUS
# ============================================================

@app.get("/status/{job_id}")
def get_status(
    job_id: str
):

    output_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_processed.mp4"
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

    # --------------------------------------------------------
    # Primero mirar si existe estado guardado
    # --------------------------------------------------------

    state = load_job_state(
        job_id
    )

    # --------------------------------------------------------
    # Si el video final existe, siempre está completado.
    # Esto permite recuperar el estado incluso después
    # de un reinicio del proceso.
    # --------------------------------------------------------

    if os.path.isfile(
        output_path
    ):

        filename = os.path.basename(
            output_path
        )

        recovered_state = {
            "status": "completed",
            "filename": filename,
            "download_url": (
                "https://resina-video-server.onrender.com"
                f"/video/{filename}"
            )
        }

        if state:

            for key in [
                "voice_duration",
                "video_duration",
                "alignment"
            ]:

                if key in state:

                    recovered_state[key] = (
                        state[key]
                    )

        return {
            "success": True,
            "job_id": job_id,
            **recovered_state
        }

    # --------------------------------------------------------
    # Si existen los archivos de entrada, el trabajo existe
    # aunque el diccionario de memoria se haya perdido.
    # --------------------------------------------------------

    if (
        os.path.isfile(input_path)
        and os.path.isfile(voice_path)
        and os.path.isfile(background_path)
    ):

        if state:

            return {
                "success": True,
                "job_id": job_id,
                **state
            }

        return {
            "success": True,
            "job_id": job_id,
            "status": "processing"
        }

    # --------------------------------------------------------
    # Si tenemos estado guardado, devolverlo
    # --------------------------------------------------------

    if state:

        return {
            "success": True,
            "job_id": job_id,
            **state
        }

    # --------------------------------------------------------
    # No existe absolutamente nada relacionado con ese job
    # --------------------------------------------------------

    raise HTTPException(
        status_code=404,
        detail="Job no encontrado"
    )
