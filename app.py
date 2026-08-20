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

jobs = {}

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")


class VideoRequest(BaseModel):
    tiktok_url: str


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


@app.post("/download_video")
def download_video(data: VideoRequest):

    if not data.tiktok_url.startswith(("http://", "https://")):
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
            os.path.join(VIDEO_DIR, f"{job_id}.*")
        )

        if not files:
            return {
                "success": False,
                "error": "El video no fue encontrado después de la descarga"
            }

        filename = os.path.basename(files[0])

        return {
            "success": True,
            "job_id": job_id,
            "filename": filename,
            "download_url": (
                f"https://resina-video-server.onrender.com/video/{filename}"
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


@app.get("/video/{filename}")
def get_video(filename: str):

    filepath = os.path.join(VIDEO_DIR, filename)

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

    body.extend(text.encode("utf-8"))
    body.extend(b"\r\n")

    body.extend(
        f"--{boundary}--\r\n".encode("utf-8")
    )

    return bytes(body), boundary


def get_forced_alignment(audio_path, text):

    if not ELEVENLABS_API_KEY:

        raise Exception(
            "Falta ELEVENLABS_API_KEY en las variables "
            "de entorno de Render"
        )

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    body, boundary = create_multipart_body(
        audio_data,
        os.path.basename(audio_path),
        text
    )

    url = "https://api.elevenlabs.io/v1/forced-alignment"

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
            "ElevenLabs Forced Alignment error "
            f"{e.code}: {error_body[-3000:]}"
        )

    except Exception as e:

        raise Exception(
            f"No se pudo obtener la sincronización "
            f"de ElevenLabs: {str(e)}"
        )


def format_srt_time(seconds):

    milliseconds = int(
        round((seconds % 1) * 1000)
    )

    total_seconds = int(seconds)

    # Corregir el caso en que el redondeo llegue a 1000 ms
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


def create_srt_from_alignment(
    alignment,
    srt_path
):

    words = alignment.get("words", [])

    if not words:
        raise Exception(
            "ElevenLabs no devolvió palabras "
            "para sincronizar"
        )

    subtitles = []

    current_words = []
    current_start = None
    current_end = None
    current_chars = 0

    MAX_WORDS = 7
    MAX_CHARS = 42

    for word_data in words:

        word = str(
            word_data.get("text", "")
        ).strip()

        if not word:
            continue

        start = float(
            word_data.get("start", 0)
        )

        end = float(
            word_data.get("end", start)
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

        should_break = (
            current_words
            and (
                len(current_words)
                >= MAX_WORDS
                or projected_chars
                > MAX_CHARS
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

        current_chars = (
            current_chars
            + len(word)
            + (
                1
                if len(current_words) > 1
                else 0
            )
        )

        # Si hay una pausa relativamente grande,
        # terminamos el subtítulo aquí.
        #
        # La pausa se determina en la siguiente palabra.
        #
        # Esto mantiene los subtítulos naturales.

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


def process_video_background(
    job_id,
    input_path,
    output_path,
    voice_path,
    background_path,
    subtitle_text
):

    try:

        jobs[job_id]["status"] = "processing"

        # --------------------------------------------------
        # 1. Obtener sincronización real de la voz
        # --------------------------------------------------

        alignment = get_forced_alignment(
            voice_path,
            subtitle_text
        )

        # --------------------------------------------------
        # 2. Crear subtítulos sincronizados
        # --------------------------------------------------

        srt_path = os.path.join(
            VIDEO_DIR,
            f"{job_id}.srt"
        )

        create_srt_from_alignment(
            alignment,
            srt_path
        )

        # --------------------------------------------------
        # 3. Procesar video + voz + música + subtítulos
        # --------------------------------------------------

        subtitle_filter_path = (
            srt_path
            .replace("\\", "/")
            .replace(":", "\\:")
            .replace("'", "\\'")
        )

        filter_complex = (
            "[1:a]volume=1.0[voice];"
            "[2:a]volume=0.20[bg];"
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
            "MarginV=60'"
            "[video]"
        )

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",

                # Video original
                "-i",
                input_path,

                # Voz ElevenLabs
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

                # El video termina con la voz
                "-shortest",

                # Streaming optimizado
                "-movflags",
                "+faststart",

                output_path
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:

            jobs[job_id]["status"] = "error"

            jobs[job_id]["error"] = (
                "FFmpeg error:\n"
                + result.stderr[-5000:]
            )

            return

        if not os.path.isfile(
            output_path
        ):

            jobs[job_id]["status"] = "error"

            jobs[job_id]["error"] = (
                "FFmpeg no generó el video"
            )

            return

        filename = os.path.basename(
            output_path
        )

        jobs[job_id]["status"] = "completed"

        jobs[job_id]["filename"] = filename

        jobs[job_id]["download_url"] = (
            "https://resina-video-server.onrender.com"
            f"/video/{filename}"
        )

        # Guardar información útil
        jobs[job_id]["alignment"] = True

    except subprocess.TimeoutExpired:

        jobs[job_id]["status"] = "error"

        jobs[job_id]["error"] = (
            "FFmpeg superó los 300 segundos"
        )

    except Exception as e:

        jobs[job_id]["status"] = "error"

        jobs[job_id]["error"] = str(e)


@app.post("/process_video")
async def process_video(
    video: UploadFile = File(...),
    voice: UploadFile = File(...),
    background: str = Form(...),
    text: str = Form(...)
):

    job_id = str(uuid.uuid4())

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

        # --------------------------------------------------
        # Guardar video
        # --------------------------------------------------

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

        # --------------------------------------------------
        # Guardar voz ElevenLabs
        # --------------------------------------------------

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

        # --------------------------------------------------
        # Descargar música
        # --------------------------------------------------

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
                "El texto de subtítulos está vacío"
            )

        if not os.path.isfile(
            voice_path
        ):

            raise Exception(
                "No se pudo guardar "
                "el audio de ElevenLabs"
            )

        jobs[job_id] = {
            "status": "queued"
        }

        # --------------------------------------------------
        # Procesamiento en segundo plano
        # --------------------------------------------------

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

        jobs[job_id] = {
            "status": "error",
            "error": str(e)
        }

        return {
            "success": False,
            "job_id": job_id,
            "status": "error",
            "error": str(e)
        }


@app.get("/status/{job_id}")
def get_status(job_id: str):

    if job_id not in jobs:

        raise HTTPException(
            status_code=404,
            detail="Job no encontrado"
        )

    job = jobs[job_id]

    return {
        "success": True,
        "job_id": job_id,
        **job
    }
