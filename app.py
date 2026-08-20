from fastapi import FastAPI
import subprocess

app = FastAPI()

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
