# The worker image. One image, two roles (see brainworker/entrypoint.py).
#
# Build context is the REPO ROOT, not worker/, because the image needs both the
# docagent engine and this package:
#     docker build -f worker/Dockerfile -t company-brain-worker:dev .

FROM python:3.13-slim

# PyMuPDF links against these at runtime. Everything else in the dependency set
# ships manylinux wheels, so there is no compiler in this image on purpose —
# a build toolchain in a shipped image is attack surface that earns nothing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# docagent first: brainworker declares it as a dependency, so installing it
# up front means the resolver finds it already present rather than trying to
# fetch a package by that name from PyPI, where it does not exist.
COPY docaget/pyproject.toml docaget/README.md* /app/docaget/
COPY docaget/docagent /app/docaget/docagent
RUN uv pip install --system --no-cache /app/docaget

COPY worker/pyproject.toml /app/worker/
COPY worker/brainworker /app/worker/brainworker
RUN uv pip install --system --no-cache --no-sources /app/worker

# The read-only operational scripts, after the install so that editing one does
# not invalidate the dependency layer. They are here because the data they
# inspect is here: `audit_version.py` reads a run's artifacts off this disk,
# which no port-forward reaches, and it needs the three store URLs that this
# container already has and a laptop has to assemble by hand. Auditing a
# production version used to mean piping a script into `docker exec` because
# the image had no copy of one.
COPY worker/scripts /app/worker/scripts

# docagent resolves profiles/, cache/ and state/ relative to the working
# directory, so the mounted workspace volume becomes their home with no change
# to its code. config.configure() chdirs here too, for the host dev mode where
# WORKDIR does not apply.
WORKDIR /workspace

ENTRYPOINT ["python", "-m", "brainworker.entrypoint"]
CMD ["worker"]
