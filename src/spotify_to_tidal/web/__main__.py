import os
import sys
import uvicorn

from .app import create_app
from .settings import WebConfigError, WebSettings


def main():
    """ Runs the web app. Configured with environment variables, see WebSettings.from_env;
        STT_HOST / STT_PORT set where it listens (default 127.0.0.1:8000) """
    try:
        settings = WebSettings.from_env()
    except WebConfigError as e:
        sys.exit(f"Error: {e}")
    uvicorn.run(create_app(settings), host=os.environ.get("STT_HOST", "127.0.0.1"), port=int(os.environ.get("STT_PORT", "8000")))


if __name__ == '__main__':
    main()
