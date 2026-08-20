from fastapi import FastAPI

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
