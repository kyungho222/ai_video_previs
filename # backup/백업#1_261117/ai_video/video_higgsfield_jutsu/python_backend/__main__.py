"""Run the local console using Python only: python -m ...python_backend."""

import argparse
import os
import uvicorn
from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="Higgsfield Python previs console")
    parser.add_argument("--port", type=int, default=4180)
    arguments = parser.parse_args()
    origin = "http://127.0.0.1:" + str(arguments.port)
    os.environ["JUTSU_OAUTH_REDIRECT_URL"] = origin + "/api/jutsu/callback"
    os.environ["JUTSU_FRONTEND_URL"] = origin + "/"
    uvicorn.run(create_app(), host="127.0.0.1", port=arguments.port)


if __name__ == "__main__":
    main()
