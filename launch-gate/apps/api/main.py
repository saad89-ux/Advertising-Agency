from fastapi import FastAPI


app = FastAPI(title="Launch Gate API")


@app.get("/health")
def health():
    return {"status": "ok"}
