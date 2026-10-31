# claimcheck CLI and service.
#   docker build -t claimcheck .
#   docker run --rm --read-only -v "$PWD:/repo:ro" claimcheck check --no-model --repo /repo --report /repo/report.txt
# See docs/SERVER.md ("Container") for the service.

# python:3.12-slim, pinned by tag and by manifest-list digest (all platforms).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

# Stage 1: build wheels for claimcheck and the [server] extra. Nothing from this stage but /wheels is kept.
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY claimcheck ./claimcheck
RUN pip wheel --wheel-dir /wheels ".[server]"

# Stage 2: runtime. No compilers, no build tools, no source tree, no pip cache.
FROM ${PYTHON_IMAGE}
# git: the report names the tested revision, and --base/--head check out revisions.
# Repos are usually bind-mounted and owned by another UID, so git is told to read them anyway.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*'
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
    pip install --no-index --find-links /wheels "claimcheck[server]"

# Run as a fixed non-root UID. The root filesystem can be mounted read-only:
# everything that writes (HOME, TMPDIR, the service's --db) points at /tmp.
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /tmp --shell /usr/sbin/nologin claimcheck
ENV HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /tmp
EXPOSE 8080
ENTRYPOINT ["claimcheck"]
CMD ["--help"]
