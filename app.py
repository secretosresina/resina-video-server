from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import os
import glob
import uuid
import threading
import urllib.request
import textwrap

app = FastAPI()

VIDEO_DIR = "/tmp/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

jobs = {}


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


def get_video_duration(video_path):

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode != 0:
        raise Exception("No se pudo obtener la duración del video")

    return float(result.stdout.strip())


def create_srt(text, duration, srt_path):

    # Limpiar espacios
    text = " ".join(text.split())

    if not text:
        raise Exception("El texto de subtítulos está vacío")

    # Dividir el texto en bloques cortos
    words = text.split()

    chunks = []
    current = []

    for word in words:

        current.append(word)

        # Aproximadamente 6-8 palabras por subtítulo
        if len(current) >= 7:
            chunks.append(" ".join(current))
            current = []

    if current:
        chunks.append(" ".join(current))

    if not chunks:
        chunks = [text]

    chunk_duration = duration / len(chunks)

    with open(srt_path, "w", encoding="utf-8") as f:

        for index, chunk in enumerate(chunks, start=1):

            start = (index - 1) * chunk_duration
            end = index * chunk_duration

            # Formato SRT
            start_time = format_srt_time(start)
            end_time = format_srt_time(end)

            # Dividir visualmente líneas demasiado largas
            wrapped = textwrap.fill(
                chunk,
                width=38
            )

            f.write(f"{index}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{wrapped}\n\n")


def format_srt_time(seconds):

    milliseconds = int((seconds % 1) * 1000)
    total_seconds = int(seconds)

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d},"
        f"{milliseconds:03d}"
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

        # Obtener duración del video
        duration = get_video_duration(input_path)

        # Crear archivo SRT
        srt_path = os.path.join(
            VIDEO_DIR,
            f"{job_id}.srt"
        )

        create_srt(
            subtitle_text,
            duration,
            srt_path
        )

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",

                # Video
                "-i", input_path,

                # Voz
                "-i", voice_path,

                # Música de fondo
                "-stream_loop", "-1",
                "-i", background_path,

                # Mezclar voz + música
                # y quemar subtítulos
                "-filter_complex",

                "[1:a]volume=1.0[voice];"
                "[2:a]volume=0.20[bg];"
                "[voice][bg]"
                "amix=inputs=2:duration=first:dropout_transition=2"
                "[audio];"
                "[0:v]"
                "subtitles="
                + srt_path.replace("\\", "/").replace(":", "\\:")
                + ":force_style="
                "'FontName=Arial,"
                "FontSize=18,"
                "PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,"
                "BorderStyle=1,"
                "Outline=3,"
                "Shadow=1,"
                "Alignment=2,"
                "MarginV=60'"
                "[video]",

                "-map", "[video]",
                "-map", "[audio]",

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",

                "-c:a", "aac",
                "-b:a", "128k",

                "-shortest",

                "-movflags", "+faststart",

                output_path
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:

            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = result.stderr[-5000:]
            return

        if not os.path.isfile(output_path):

            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = (
                "FFmpeg no generó el video"
            )
            return

        filename = os.path.basename(output_path)

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["filename"] = filename
        jobs[job_id]["download_url"] = (
            f"https://resina-video-server.onrender.com/video/{filename}"
        )

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

        # Guardar video
        with open(input_path, "wb") as f:

            while True:

                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                f.write(chunk)

        # Guardar voz
        with open(voice_path, "wb") as f:

            while True:

                chunk = await voice.read(1024 * 1024)

                if not chunk:
                    break

                f.write(chunk)

        # Descargar música de fondo
        urllib.request.urlretrieve(
            background,
            background_path
        )

        if not os.path.isfile(background_path):

            jobs[job_id] = {
                "status": "error",
                "error": (
                    "No se pudo descargar "
                    "el sonido de fondo"
                )
            }

            return {
                "success": False,
                "job_id": job_id,
                "status": "error",
                "error": (
                    "No se pudo descargar "
                    "el sonido de fondo"
                )
            }

        if not text.strip():

            jobs[job_id] = {
                "status": "error",
                "error": "El texto está vacío"
            }

            return {
                "success": False,
                "job_id": job_id,
                "status": "error",
                "error": "El texto está vacío"
            }

        jobs[job_id] = {
            "status": "queued"
        }

        # Procesar en segundo plano
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
