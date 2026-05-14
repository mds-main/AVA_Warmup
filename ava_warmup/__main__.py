"""Run the standalone AVA Spec Warm Up web app.

This entrypoint uses Flask's development server and is intended for local
development only. For production on DigitalOcean App Platform (or any
PaaS), use the gunicorn command in `Procfile` instead.
"""

from .config import load_app_config
from .web_app import app


def main() -> None:
    config = load_app_config()
    app.run(host=config.server_host, port=config.server_port, debug=False)


if __name__ == "__main__":
    main()
