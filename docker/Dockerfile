# omp: the OrderMyPhotos runner. CPU stages plus PyTorch XPU for the Arc Pro B50 (falls back to CPU).
# Built on Mimir from /srv/mimir/stacks/photos/src (a copy of this repo):
#   docker build -t mimir/omp:0.1 -f docker/Dockerfile .
# The first layers are identical to mimir's stacks/depth image, so they come from the build cache.
FROM ubuntu:26.04

# Level Zero / OpenCL user-space driver for the B50 (same 26.05 compute runtime as the host)
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates libze-intel-gpu1 libze1 intel-opencl-icd \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN uv venv --python 3.13 /opt/venv \
 && uv pip install --no-cache --index-url https://download.pytorch.org/whl/xpu \
      torch==2.15.0+xpu torchvision==0.29.1+xpu \
 && uv pip install --no-cache \
      transformers==5.22.1 fastapi==0.162.0 uvicorn==0.54.0 pillow==13.0.0 python-multipart==0.0.33 "numpy<3"

# exiftool for S0; a compiler for insightface's Cython extension
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libimage-exiftool-perl build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt /tmp/requirements.txt
# insightface depends on the GUI build of OpenCV (needs X11 libs); swap it for the headless one
RUN uv pip install --no-cache -r /tmp/requirements.txt \
 && uv pip uninstall opencv-python \
 && uv pip install --no-cache --reinstall opencv-python-headless==5.0.0.93

COPY pyproject.toml /src/
COPY src /src/src
RUN uv pip install --no-cache --no-deps /src

ENV HOME=/tmp HF_HOME=/models/hf HF_HUB_DISABLE_TELEMETRY=1 PYTHONUNBUFFERED=1
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "omp"]
