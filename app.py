from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import os
import glob
import uuid
import threading

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
            "download_url": f"https://resina-video-server.onrender.com/video/{filename}"
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


def process_video_background(job_id, input_path, output_path):

    try:

        jobs[job_id]["status"] = "processing"

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                output_path
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:

            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = result.stderr[-4000:]
            return

        if not os.path.isfile(output_path):

            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "FFmpeg no generó el video"
            return

        filename = os.path.basename(output_path)

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["filename"] = filename
        jobs[job_id]["download_url"] = (
            f"https://resina-video-server.onrender.com/video/{filename}"
        )

    except subprocess.TimeoutExpired:

        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "FFmpeg superó los 300 segundos"

    except Exception as e:

        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.post("/process_video")
async def process_video(video: UploadFile = File(...)):

    job_id = str(uuid.uuid4())

    input_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_input.mp4"
    )

    output_path = os.path.join(
        VIDEO_DIR,
        f"{job_id}_processed.mp4"
    )

    try:

        # Guardar el video recibido
        with open(input_path, "wb") as f:

            while True:

                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                f.write(chunk)

        jobs[job_id] = {
            "status": "queued"
        }

        # Lanzar FFmpeg en segundo plano
        thread = threading.Thread(
            target=process_video_background,
            args=(job_id, input_path, output_path),
            daemon=True
        )

        thread.start()

        # Responder inmediatamente a Make
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
