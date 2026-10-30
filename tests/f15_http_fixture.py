"""Controlled loopback fixture used by the Go signer integration test only."""
import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess

from fastapi import FastAPI, Request
import uvicorn
from app.core.security import WorkerAuthMiddleware


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    middleware = WorkerAuthMiddleware
    if args.baseline:
        # Immutable pre-F15 receiver for sender-first compatibility proof.
        source = subprocess.check_output(["git", "show", "18c96a36271c0f6269d33ca5aac6e82e4ab964cc:app/core/security.py"], cwd=Path(__file__).parents[1])
        namespace = {"__name__": "f15_old_worker_security"}
        exec(compile(source, "pre_f15_security.py", "exec"), namespace)
        middleware = namespace["WorkerAuthMiddleware"]
    os.environ.pop("REDIS_URL", None)
    app = FastAPI()
    app.add_middleware(middleware)

    @app.get("/health/live")
    async def health():
        return {"status": "alive"}

    @app.api_route("/probe", methods=["GET", "POST"])
    async def probe(request: Request):
        body = await request.body()  # Small test fixture only, never production middleware.
        result = {"size": len(body), "digest": hashlib.sha256(body).hexdigest()}
        if request.headers.get("content-type", "").startswith("multipart/"):
            async with request.form() as form:
                file = form["file"]
                result.update(language=form["language"], file_size=len(await file.read()))
        return result

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    print(listener.getsockname()[1], flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[listener])


if __name__ == "__main__":
    main()
