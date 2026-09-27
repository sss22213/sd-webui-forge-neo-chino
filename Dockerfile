# sd-webui-forge-neo (Forge Neo) container image.
#
# Base: NVIDIA CUDA 13.0 + cuDNN runtime on Ubuntu 24.04, matching the
# `torch==2.13.0+cu130` build that launch_utils.py installs.
# If your host driver is older, downgrade the tag (e.g. 12.6.x) and
# adjust TORCH_INDEX_URL / TORCH_COMMAND in docker-compose.yml.
#
# Everything launch.py would otherwise pip-install on boot is baked in
# here: torch/xformers, gradio, requirements.txt, AND every extension install.py
# dependency. Packages installed at runtime land in the container's
# writable layer and vanish whenever the container is recreated, which
# is why `docker compose up` used to spend minutes re-installing them.
#
# Layers are ordered rarely-changing -> often-changing so `--build`
# reuses as much cache as possible. pip downloads live in a BuildKit
# cache mount, so even an invalidated layer does not re-download wheels.
#
# If you add an extension that ships an install.py, add its packages to
# the "extension deps" layer below, or they will be re-installed on
# every container recreate again.
FROM nvidia/cuda:13.0.1-cudnn-runtime-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_PREFER_BINARY=1 \
    PIP_ROOT_USER_ACTION=ignore

# System deps + Python 3.13 (Forge Neo requires 3.13.x).
# libcairo2-dev + pkg-config are needed so pip can build pycairo, which
# svglib transitively requires for the ControlNet SVG preprocessor.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git bc \
        software-properties-common gnupg \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
        libgoogle-perftools4 \
        libcairo2-dev pkg-config \
        build-essential \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        python3.13 python3.13-venv python3.13-dev \
 && curl -fsSL https://bootstrap.pypa.io/get-pip.py | python3.13 \
 && ln -sf /usr/bin/python3.13 /usr/local/bin/python \
 && ln -sf /usr/bin/python3.13 /usr/local/bin/python3 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Bind-mounted extension repos are owned by the host UID, which trips
# git's "dubious ownership" guard inside the container. Trust everything
# under /workspace so Forge's extension scanner can read remote URLs.
RUN git config --system --add safe.directory '*'

WORKDIR /workspace

# ---------------------------------------------------------------------
# Layer 1: torch + torchvision + xformers. Changes almost never.
# Versions mirror modules/launch_utils.py:prepare_environment().
# ---------------------------------------------------------------------
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install --extra-index-url "${TORCH_INDEX_URL}" \
        torch==2.13.0+cu130 torchvision==0.28.0+cu130 \
 && python -m pip install --extra-index-url "${TORCH_INDEX_URL}" \
        xformers==0.0.35

# ---------------------------------------------------------------------
# Layer 1b: gradio. NOT in requirements.txt; launch.py installs it
# itself (`if not is_installed("gradio")`), so without this layer every
# container recreate printed "Installing gradio".
# Version mirrors GRADIO_PACKAGE in launch_utils.py.
# gradio 4.40 pins pillow<11; the requirements.txt layer below re-pins
# Pillow afterwards, exactly like launch.py does at boot.
# (launch.py also has a `packaging==26.2` fallback, but the apt layer
# already provides Debian's python3-packaging, which pip cannot
# uninstall, and launch.py only checks presence, so leave it alone.)
# ---------------------------------------------------------------------
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install gradio==4.40.0 gradio_rangeslider==0.0.8

# ---------------------------------------------------------------------
# Layer 2: project requirements.txt.
# ---------------------------------------------------------------------
COPY requirements.txt /tmp/deps/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r /tmp/deps/requirements.txt

# ---------------------------------------------------------------------
# Layer 3: extension deps. Mirrors, in the same order launch.py runs
# them, what each install.py does on boot:
#   extensions/Stable-Diffusion-Webui-Civitai-Helper/install.py -> pysocks
#   extensions/sd-dynamic-prompts/install.py -> [project].dependencies
#       from its pyproject.toml
#   extensions-builtin/forge_legacy_preprocessors/install.py ->
#       its requirements.txt + insightface + two Depth-Anything wheels
# Then requirements.txt is applied once more, exactly like launch.py's
# "enforce requirements" step, so any pin an installer moved is put
# back and the boot-time requirements_met() check is a no-op.
# ---------------------------------------------------------------------
COPY extensions/sd-dynamic-prompts/pyproject.toml \
     /tmp/deps/sd-dynamic-prompts.toml
COPY extensions-builtin/forge_legacy_preprocessors/requirements.txt \
     /tmp/deps/legacy_preprocessors.txt
ARG DEPTH_ANYTHING_WHEEL=https://github.com/huchenlei/Depth-Anything/releases/download/v1.0.0/depth_anything-2024.1.22.0-py2.py3-none-any.whl
ARG DEPTH_ANYTHING_V2_WHEEL=https://github.com/MackinationsAi/UDAV2-ControlNet/releases/download/v1.0.0/depth_anything_v2-2024.7.1.0-py2.py3-none-any.whl
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install pysocks \
 && python -c "import tomllib; d = tomllib.load(open('/tmp/deps/sd-dynamic-prompts.toml', 'rb'))['project']['dependencies']; open('/tmp/deps/sd-dynamic-prompts.txt', 'w').write(chr(10).join(d) + chr(10))" \
 && python -m pip install -r /tmp/deps/sd-dynamic-prompts.txt \
 && python -m pip install -r /tmp/deps/legacy_preprocessors.txt \
 && python -m pip install insightface \
 && python -m pip install "${DEPTH_ANYTHING_WHEEL}" "${DEPTH_ANYTHING_V2_WHEEL}" \
 && python -m pip install -r /tmp/deps/requirements.txt

# ---------------------------------------------------------------------
# Optional extras. Only needed if you put the matching flag in
# COMMANDLINE_ARGS; otherwise launch.py never asks for them.
#   --pynvml -> nvidia-ml-py==13.610.43
#   --sage  -> triton==3.7.1 sageattention==2.2.0
#   --flash -> flash_attn wheel (see launch_utils.py for the URL)
# Example:
# RUN --mount=type=cache,target=/root/.cache/pip \
#     python -m pip install nvidia-ml-py==13.610.43
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Verify: replay the exact checks launch.py runs at boot
# (requirements_met() on requirements.txt, is_installed() for every
# extension package, dynamic-prompts' specifier check). If any of them
# would trigger a pip run at boot, fail the build now instead.
# ---------------------------------------------------------------------
RUN python - <<'PY' && rm -rf /tmp/deps
import importlib.metadata as md
import importlib.util
import re
import sys

from packaging.requirements import Requirement
from packaging.version import parse

bad = []

# modules/launch_utils.py:requirements_met()
re_requirement = re.compile(r"\s*(\S+)\s*==\s*([^\s;]+)\s*")
for line in open("/tmp/deps/requirements.txt", encoding="utf8"):
    m = re_requirement.match(line)
    if m is None:
        continue
    name, want = m.group(1), m.group(2)
    try:
        have = md.version(name)
    except Exception:
        bad.append(f"requirements.txt: {name} not installed")
        continue
    if parse(have) < parse(want):
        bad.append(f"requirements.txt: {name} {have} < {want}")


# modules/launch_utils.py:is_installed()
def is_installed(package):
    try:
        md.distribution(package)
        return True
    except md.PackageNotFoundError:
        pass
    try:
        return importlib.util.find_spec(package) is not None
    except ModuleNotFoundError:
        return False


# prepare_environment() itself: torch/torchvision, packaging, xformers (--xformers), gradio
core_packages = ["torch", "torchvision", "packaging", "xformers", "gradio", "gradio_rangeslider"]
bad += [f"launch.py dep missing: {p}" for p in core_packages if not is_installed(p)]

ext_packages = ["pysocks", "insightface", "depth_anything", "depth_anything_v2"]
ext_packages += [l.strip() for l in open("/tmp/deps/legacy_preprocessors.txt") if l.strip()]
bad += [f"extension dep missing: {p}" for p in ext_packages if not is_installed(p)]

# extensions/sd-dynamic-prompts/sd_dynamic_prompts/version_tools.py
for req in map(Requirement, open("/tmp/deps/sd-dynamic-prompts.txt").read().split()):
    try:
        have = md.version(req.name)
    except Exception:
        have = None
    if not (have and req.specifier.contains(have)):
        bad.append(f"sd-dynamic-prompts: {req} (installed: {have})")

if bad:
    print("Baked image would still trigger pip installs at boot:", *bad, sep="\n  ")
    sys.exit(1)
print("All boot-time dependency checks satisfied; launch.py will not pip install anything.")
PY

# launch.py reads COMMANDLINE_ARGS (see modules/paths_internal.py).
# webui.sh's venv logic is bypassed; we run the system interpreter.
# PIP_NO_CACHE_DIR is set only from here on (runtime), so an ad-hoc
# install by a newly added extension does not bloat the container
# layer; the build stages above use the BuildKit cache mount instead.
ENV HF_HOME=/workspace/cache/huggingface \
    TORCH_HOME=/workspace/cache/torch \
    XDG_CACHE_HOME=/workspace/cache \
    PIP_USER=0 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

EXPOSE 7860

CMD ["python", "launch.py"]
