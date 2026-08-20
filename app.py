from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import os
import glob
import uuid

app = FastAPI()

VIDEO_DIR = "/tmp/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)


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

        version = result.stdout.splitlines()[0] if result.stdout else "FFmpeg encontrado"

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
            "download_url": f"/video/{filename}"
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "La descarga superó el límite de 180 segundos"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@app.get("/video/{filename}")
def get_video(filename: str):

    from fastapi.responses import FileResponse

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
