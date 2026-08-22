# The image commands run in when CODER_SANDBOX_MODE=docker. Deliberately tiny: the repo is
# mounted at /work at run time, so the image only needs an interpreter and a test runner.
# Built on first use by `coder_agent.sandbox.docker.ensure_image`, or by hand:
#   docker build -t coder-sandbox <this directory>
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NO_COLOR=1 PIP_NO_CACHE_DIR=1
RUN pip install --quiet "pytest>=8"
WORKDIR /work
