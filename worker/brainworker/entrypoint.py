"""Container entrypoint: one image, two roles.

    python -m brainworker.entrypoint worker   # Temporal worker
    python -m brainworker.entrypoint api      # FastAPI control plane

Both roles share the image, the workspace mount and the secrets mount; keeping
them in separate containers means a worker crash-loop cannot take the UI's
control plane down with it.
"""

from __future__ import annotations

import sys

USAGE = "usage: python -m brainworker.entrypoint {worker|api}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2

    role = args[0]
    if role == "worker":
        from .runner import run

        run()
        return 0
    if role == "api":
        import uvicorn

        from . import config

        settings = config.configure()
        uvicorn.run(
            "brainworker.api.main:app",
            host=settings.api_host,
            port=settings.api_port,
            log_level=settings.log_level.lower(),
        )
        return 0

    print(f"unknown role {role!r}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
