# Forge Neo Chino

**sd-webui-forge-neo-chino** is a modified version of Stable Diffusion WebUI Forge Neo that adds a fully pre-installed Docker image, mobile browser support, a model unload API, and a LoRA import API.

This repository is a personal modification of the `neo` branch of [Haoming02/sd-webui-forge-classic](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo), also known as **Forge Neo**, maintained by [sss22213](https://github.com/sss22213). "Chino" is only a codename for this variant and is not affiliated with the upstream project.

Compared with upstream, it adds four things:

- **One-step Docker build**: every Python package is installed during `docker compose build`, so `docker compose up` no longer waits for installs.
- **Usable on mobile browsers**: after locking the screen or switching apps, the page reattaches to the running generation and fetches its result. Several touch and layout problems are also fixed.
- **Model unload API**: unloading can optionally release system RAM as well, and a new endpoint reloads the model.
- **LoRA import API**: other programs can add a LoRA by URL or upload, with its card text and preview, without a restart.

> [!IMPORTANT]
> This is not an official release. The upstream author has stated that issues caused by third-party modifications will be ignored, so please report problems with this repository here. Before reporting anything upstream, reproduce it on the [official version](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo) first.

For the full upstream feature list, supported models, and command-line arguments, see the [official Forge Neo README](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo#readme).

<br>

## Credits

### Source code

The code in this repository builds on the following projects, in order. All of them are licensed under AGPL-3.0.

| Project | Authors | Relationship to this repository |
| --- | --- | --- |
| [Stable Diffusion WebUI](https://github.com/AUTOMATIC1111/stable-diffusion-webui) | AUTOMATIC1111 and contributors | The original WebUI; source of the interface and most features |
| [Stable Diffusion WebUI Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge) | lllyasviel and contributors | Rewrote the backend and VRAM management on top of A1111 |
| [Forge Classic / Neo](https://github.com/Haoming02/sd-webui-forge-classic) | Haoming02 and contributors | Continues Forge and adds support for new models; **this repository is based directly on its `neo` branch** |

The Forge Neo README also gives special thanks to comfyanonymous (ComfyUI), kijai, city96, and others. The backend code inherited by this repository benefits from their work as well.

### Base version

This repository is based on upstream `neo` branch commit [`3a3dea7f`](https://github.com/Haoming02/sd-webui-forge-classic/commit/3a3dea7ff781f525dd227c0f6f9ed2790ad00a35) (2026-09-26). Apart from the files listed under [Modifications](#modifications), all code is identical to upstream.

### Third-party extensions

The following extensions are **not included in this repository**, because the `extensions/` folder is excluded by `.gitignore`. The installation steps explain how to get them.

| Extension | Authors | License | Notes |
| --- | --- | --- | --- |
| [sd-dynamic-prompts](https://github.com/adieyal/sd-dynamic-prompts) | Adi Eyal (adieyal) | MIT | The Docker build reads its `pyproject.toml`, so it **must be cloned first** |
| [sd-webui-prompt-history](https://github.com/namkazt/sd-webui-prompt-history) | Nguyen Dac Nam (namkazt) | MIT | Optional |
| [Civitai Helper (sss22213 fork)](https://github.com/sss22213/Stable-Diffusion-Webui-Civitai-Helper) | Originally by [butaixianran](https://github.com/butaixianran/Stable-Diffusion-Webui-Civitai-Helper); fork maintained by sss22213 | No license file in the original project | Optional. The fork keeps version 1.x working on Forge Classic / Neo with Gradio 4 and adds an HTTP API |

### Runtime environment

| Component | Source |
| --- | --- |
| Container base image | [nvidia/cuda](https://hub.docker.com/r/nvidia/cuda) `13.0.1-cudnn-runtime-ubuntu24.04`, under the NVIDIA Deep Learning Container License |
| Python 3.13 | Ubuntu [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) |
| PyTorch / xformers | `torch 2.11.0+cu130`, `torchvision 0.26.0+cu130`, `xformers 0.0.35`, matching upstream `launch_utils.py` |
| Gradio | `4.40.0`, matching upstream |

<br>

## Modifications

Changes were made between 2026-07 and 2026-10; last updated 2026-10-17. Each item lists the affected files for easy comparison with upstream.

### 1. Docker deployment

New files: `Dockerfile`, `docker-compose.yml`, `.dockerignore`

Upstream `launch.py` checks for missing packages on every start and installs them with pip. Inside a container, those packages land in the container's writable layer and disappear whenever the container is recreated, so every `docker compose up` spent minutes reinstalling them. This repository moves all of those installs into the image build:

- **Core packages**: torch, torchvision, xformers, gradio, gradio_rangeslider, and `requirements.txt`.
- **Extension `install.py` dependencies**: pysocks for Civitai Helper; dynamicprompts and send2trash for sd-dynamic-prompts; mediapipe, onnxruntime, insightface, timm, fvcore, svglib, and two Depth-Anything wheels for the built-in ControlNet preprocessors.
- **Self-check at build time**: the last build step re-runs the exact checks `launch.py` performs at startup. If anything would still trigger a pip install at startup, the build fails.
- **Build cache**: pip uses a BuildKit cache mount, so rebuilding an invalidated layer does not download wheels again.
- **System dependencies**: `libcairo2-dev` is pre-installed so pycairo, required by svglib, can compile. git `safe.directory` is set to avoid ownership errors on bind-mounted folders.
- **docker-compose**: GPU reservation, 8 GB shared memory, and a health check. The source tree is bind-mounted, so changes to Python, JS, or CSS only need a container restart, not a rebuild.

### 2. API

| Endpoint | Change | Files |
| --- | --- | --- |
| `POST /sdapi/v1/unload-checkpoint` | New `full` query parameter; now runs inside the queue lock | `modules/api/api.py`, `modules/sd_models.py` |
| `POST /sdapi/v1/reload-checkpoint` | **New**. Loads the currently selected model and returns `{"reloaded": true/false}` | `modules/api/api.py` |
| `POST /internal/progress` | New `preview_max_size` field that downscales the live preview on the server | `modules/progress.py` |
| `POST /sdapi/v1/lora/import` | **New**. Downloads a `.safetensors` from a URL into the Lora folder | `extensions-builtin/sd_forge_lora/lora_import.py` |
| `POST /sdapi/v1/lora/upload` | **New**. Same, with the file uploaded as multipart form data | same |
| `GET /sdapi/v1/lora/import` | **New**. Reports that the import API is available, the Lora folder and its sub folders | same |

The `full` parameter of `unload-checkpoint`:

- `full=false` (default): only moves weights out of VRAM. The model stays in system RAM, so the next generation starts faster.
- `full=true`: also releases the model object, freeing system RAM. The next generation automatically reloads the same model from disk.

> [!NOTE]
> **Behavior difference from upstream**: in the latest upstream, calling `unload-checkpoint` without parameters performs a full unload. In this repository it only clears VRAM; add `?full=true` for a full unload. The unload button under Settings > Actions behaves the same way.

The automatic cleanup after a VRAM out-of-memory error still performs a full unload, matching upstream. File: `modules/ui.py`.

**LoRA import.** Made for tools that train LoRAs elsewhere (for example [LoRA Tag Studio](https://github.com/sss22213/LoRA-Tag-Studio), which trains on Civitai) and want the result in Forge without copying files by hand.

- Fields (JSON for `import`, form fields for `upload`): `url` (import only), `filename`, `subfolder`, `activation_text`, `preferred_weight`, `negative_text`, `description`, `notes`, `sd_version` (a preset name such as `xl`, `flux`, `anima`, or `Unknown`), `preview_url`, `overwrite`, `sha256`.
- The file must be a valid safetensors file; its header is parsed and nothing is executed. File and sub folder names are sanitized and cannot leave the Lora folder.
- An identical existing file returns `"status": "exists"`. A different file with the same name returns HTTP 409 unless `overwrite` is true.
- The card metadata goes into `<name>.json` (the same file the "Edit metadata" dialog writes), so clicking the card adds the activation text and weight. `preview_url` is saved as `<name>.preview.<ext>`; a preview that cannot be fetched does not fail the import.
- Only the new file is registered, so it appears in the Lora tab after pressing refresh, without rescanning every LoRA or restarting.
- The response includes `name`, `alias`, `relative_path`, `sha256`, and a ready-made `prompt` such as `<lora:name:0.8> trigger`.
- Like every other `/sdapi` endpoint it has no authentication of its own. With `--listen`, anyone who can reach the port can add files to the Lora folder; use `--api-auth` or a firewall if the port is reachable from untrusted machines.
- The file registration is in `extensions-builtin/sd_forge_lora/scripts/lora_script.py`; the logic is in `lora_import.py` next to it.

**MCP server** (added 2026-10-17). A new built-in extension, `extensions-builtin/sd_forge_mcp`, serves the WebUI as a [Model Context Protocol](https://modelcontextprotocol.io) server at `/mcp` (Streamable HTTP), so AI agents such as Claude Code, Claude Desktop or Codex can use it directly.

- Tools: `txt2img`, `img2img` (both stream progress notifications while generating and return the images plus the generation info), `list_checkpoints`, `set_checkpoint`, `list_samplers`, `list_schedulers`, `list_upscalers`, `list_loras` (`full: true` adds the path and training metadata), `list_modules`, `list_embeddings`, `list_styles`, `png_info`, `get_progress` (optionally with the live preview), `interrupt`, `skip`, `refresh_checkpoints`, `refresh_loras`, `refresh_embeddings`, `a1111_memory`, `a1111_unload_checkpoint`, `a1111_reload_checkpoint`.
- Each tool calls the WebUI's own `/sdapi/v1` route in-process, so the results match the REST API and `--api` is required. `txt2img` / `img2img` accept `checkpoint` (used for that request only), `extra` (any other request field, e.g. `alwayson_scripts`) and `image_format: "jpeg"` for smaller results.
- Authentication: clients send `Authorization: Bearer <token>` (or `X-MCP-Token`, or `?token=` for clients that cannot set headers). The token is generated on first start and shown under **Settings > MCP Server**, where the server can also be turned off; the environment variable `FORGE_MCP_TOKEN` overrides it. With `--api-auth`, the in-process calls reuse the first credentials.
- No new dependencies: the protocol is implemented in `lib_mcp/server.py` on top of FastAPI (the official `mcp` Python SDK needs a newer pydantic than this WebUI pins). The server is stateless, offers no server-initiated stream (`GET` returns 405), and answers a call with a progress token as an SSE stream.

### 3. Mobile support

**Reattach to the running generation after locking the screen.** When a phone locks its screen or switches apps, the browser freezes the page. Upstream progress polling stopped on the first failed request, so users returned to a frozen interface.

- `javascript/progressbar.js`: when the page is hidden, progress tracking is paused instead of ended. On return, it reattaches to the task through the "restore progress" path and fetches the result from the server.
- `javascript/progressbar.js`: time away is measured with `Date.now()`. `performance.now()` pauses during phone deep sleep, which made a long screen-off look short.
- `javascript/progressbar.js`: transient network errors are retried twice. A failed live preview no longer tears down the whole progress bar. The screen wake lock is re-acquired on the next update after the browser releases it.
- `javascript/ui.js`: on page load, unfinished tasks saved in the browser are checked and restored automatically. Tasks the server no longer knows about are discarded. Submitting a new task hides the old restore button, so two progress bars cannot appear at once.
- `modules/shared_options.py`: new setting "Restore progress when returning to the page" in the Live Previews section of Settings, enabled by default.

**Downscaled live previews.** Full-size preview images are sent as data URIs, which can get a mobile browser tab killed. `javascript/progressbar.js` computes the size actually needed from the gallery's displayed size and the device pixel ratio, capped at 4096 pixels, and `modules/progress.py` downscales the image on the server before sending it.

**Browser compatibility**:

- `javascript/notification.js`: iOS Safari has no `Notification` API outside installed PWAs, which used to throw an error. Errors from mobile browsers blocking the notification sound's autoplay are also caught.
- `javascript/extraNetworks.js`: `navigator.clipboard` only exists over HTTPS, while phones usually connect over plain HTTP on the LAN. A fallback copy method makes "copy model path" work.
- `extensions-builtin/mobile/javascript/mobile.js`: a missing element during the mobile layout switch used to break the layout switch permanently; it is now guarded.

**Touch and layout** in `style.css` and `extensions-builtin/sd_forge_controlnet/style.css`, applied only at phone widths or on touch devices:

- Dragging the panel divider no longer scrolls the page.
- The LoRA and other model panes had a minimum height of 52rem, taller than a phone screen. They now size to the visible viewport and account for the collapsing iOS address bar.
- Touch devices have no hover, so the action buttons on model cards are always visible.
- The Settings sidebar stacks above the content, model selectors are wider, tab rows wrap, and the search and sort fields no longer cause horizontal scrolling.
- The metadata popup, the edit dialog, and accordion rows no longer overflow the screen width.
- The small buttons over ControlNet previews are enlarged to finger size.

### 4. Legacy extension compatibility

- New `modules/generation_parameters_copypaste.py`: Forge Neo renamed this module to `modules.infotext_utils`, so many older extensions that import the old name fail to load. This file re-exports the new module unchanged.

### 5. Bug fixes

| File | Problem | Fix |
| --- | --- | --- |
| `modules/ui_tempdir.py` | Saving images failed when the temp folder set in Settings did not exist, because the folder was only created for the default location | Create the folder in every case |
| `extensions-builtin/forge_preprocessor_inpaint/lama/modules/base.py` | `raise NotImplemented()` is not callable and actually raises TypeError | Changed to `NotImplementedError()` |
| `modules/scripts.py` | `ScriptRunner.process_before_every_sampling` was defined twice, and the second definition overrode the first | Removed the duplicate and kept the version that follows the user's script order, consistent with the other hooks |
| `backend/patcher/controlnet.py` | The T2I-Adapter prefix mapping passed an unused loop variable to `format()` | Removed the extra argument; the result is unchanged |

### Changed files

| File | Status | Category |
| --- | --- | --- |
| `Dockerfile` | Added | Docker |
| `docker-compose.yml` | Added | Docker |
| `.dockerignore` | Added | Docker |
| `modules/generation_parameters_copypaste.py` | Added | Compatibility |
| `modules/api/api.py` | Modified | API |
| `modules/sd_models.py` | Modified | API |
| `modules/ui.py` | Modified | API |
| `modules/progress.py` | Modified | API, mobile |
| `extensions-builtin/sd_forge_lora/lora_import.py` | Added | API |
| `extensions-builtin/sd_forge_lora/scripts/lora_script.py` | Modified | API |
| `extensions-builtin/sd_forge_mcp/scripts/forge_mcp.py` | Added | API (MCP server) |
| `extensions-builtin/sd_forge_mcp/lib_mcp/server.py` | Added | API (MCP server) |
| `extensions-builtin/sd_forge_mcp/lib_mcp/a1111_tools.py` | Added | API (MCP server) |
| `extensions-builtin/sd_forge_mcp/lib_mcp/__init__.py` | Added | API (MCP server) |
| `modules/shared_options.py` | Modified | Mobile |
| `javascript/progressbar.js` | Modified | Mobile |
| `javascript/ui.js` | Modified | Mobile |
| `javascript/notification.js` | Modified | Mobile |
| `javascript/extraNetworks.js` | Modified | Mobile |
| `extensions-builtin/mobile/javascript/mobile.js` | Modified | Mobile |
| `style.css` | Modified | Mobile |
| `extensions-builtin/sd_forge_controlnet/style.css` | Modified | Mobile |
| `modules/ui_tempdir.py` | Modified | Bug fix |
| `modules/scripts.py` | Modified | Bug fix |
| `extensions-builtin/forge_preprocessor_inpaint/lama/modules/base.py` | Modified | Bug fix |
| `backend/patcher/controlnet.py` | Modified | Bug fix |
| `README.md` | Rewritten | Documentation |
| `.gitignore` | Modified | Excludes the local `.claude/` settings folder |

<br>

## Installation: Docker

### Requirements

- Linux with an NVIDIA GPU whose driver supports CUDA 13.0.
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
- Docker and Docker Compose v2. BuildKit is enabled by default.

If your driver does not support CUDA 13.0, change both the base image tag in `Dockerfile` and the torch and xformers versions.

### Steps

**1. Clone this repository**

```bash
git clone https://github.com/sss22213/sd-webui-forge-neo-chino.git
cd sd-webui-forge-neo-chino
```

**2. Clone the extensions.** sd-dynamic-prompts is required for the build; the other two are optional.

```bash
git clone https://github.com/adieyal/sd-dynamic-prompts.git extensions/sd-dynamic-prompts
git clone https://github.com/namkazt/sd-webui-prompt-history.git extensions/sd-webui-prompt-history
git clone https://github.com/sss22213/Stable-Diffusion-Webui-Civitai-Helper.git extensions/Stable-Diffusion-Webui-Civitai-Helper
```

**3. Adjust the mount paths.** The `volumes` section of `docker-compose.yml` contains two disk paths from the maintainer's machine. Change them to your own model and output folders, or delete them to use the project's `models/` and `output/` folders.

```yaml
    volumes:
      - .:/workspace
      - /mnt/ai/model:/workspace/models                                        # your model folder
      - /media/disk1/ai_output/txt2img-images:/workspace/output/txt2img-images # your output folder
```

**4. Build and start.** The first build downloads several GB of packages and takes a while.

```bash
docker compose build
docker compose up -d
docker logs -f sd-webui-forge-neo
```

Then open `http://localhost:7860` in a browser. From a phone, use `http://<your computer's LAN IP>:7860`.

**5. Confirm nothing is installed at startup.** The following command should print nothing.

```bash
docker logs sd-webui-forge-neo 2>&1 | grep -i installing
```

### When to rebuild

| What changed | What to run |
| --- | --- |
| Python, JavaScript, or CSS source | `docker compose restart` |
| Environment variables or launch arguments in `docker-compose.yml` | `docker compose up -d` |
| `Dockerfile` or `requirements.txt` | `docker compose build`, then `docker compose up -d` |
| A new extension that has an `install.py` | Add its packages to the "extension deps" layer in `Dockerfile`, then rebuild |

If the last case is skipped, the new extension's packages are reinstalled every time the container is recreated, bringing back the slow startup.

### Without Docker

Installation is the same as upstream; see the [Forge Neo installation guide](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo#installation). The API and mobile changes in this repository work outside Docker as well.

<br>

## API examples

The launch arguments must include `--api`, which `docker-compose.yml` already sets.

```bash
# Clear VRAM only; the model stays in system RAM
curl -X POST http://127.0.0.1:7860/sdapi/v1/unload-checkpoint

# Free both VRAM and system RAM; the next generation reloads automatically
curl -X POST "http://127.0.0.1:7860/sdapi/v1/unload-checkpoint?full=true"

# Manually reload the currently selected model
curl -X POST http://127.0.0.1:7860/sdapi/v1/reload-checkpoint

# Check memory usage to confirm the unload worked
curl http://127.0.0.1:7860/sdapi/v1/memory

# Is the LoRA import API available, and where do files go?
curl http://127.0.0.1:7860/sdapi/v1/lora/import

# Download a LoRA into models/Lora/my-loras/ with its card text and preview
curl -X POST http://127.0.0.1:7860/sdapi/v1/lora/import -H "Content-Type: application/json" -d '{
  "url": "https://example.com/my_character.safetensors",
  "filename": "my_character_e10",
  "subfolder": "my-loras",
  "activation_text": "my character",
  "preferred_weight": 0.8,
  "sd_version": "xl",
  "preview_url": "https://example.com/sample.jpg"
}'

# Upload a local file instead
curl -X POST http://127.0.0.1:7860/sdapi/v1/lora/upload \
  -F file=@my_character_e10.safetensors -F subfolder=my-loras -F activation_text="my character"
```

Connect an MCP client (the token is under Settings > MCP Server):

```bash
# Claude Code
claude mcp add --transport http forge http://127.0.0.1:7860/mcp --header "Authorization: Bearer <token>"

# Codex: the token is read from an environment variable when codex runs
codex mcp add forge --url http://127.0.0.1:7860/mcp --bearer-token-env-var FORGE_MCP_TOKEN
export FORGE_MCP_TOKEN=<token>

# Raw JSON-RPC
curl -X POST http://127.0.0.1:7860/mcp -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

<br>

## Syncing with upstream

This repository does not carry upstream's git history, so `git merge` cannot be used. Instead, apply upstream's changes since the [base version](#base-version) as a patch. Replace `3a3dea7f` below with the commit currently listed under Base version.

```bash
git remote add upstream https://github.com/Haoming02/sd-webui-forge-classic.git
git fetch upstream
git diff --binary 3a3dea7f upstream/neo -- . ':!README.md' | git apply --3way
```

- Files that both upstream and this repository changed may get conflict markers. Resolve them, then commit.
- After committing, update the commit and date under [Base version](#base-version) to the output of `git log -1 --format='%h %as' upstream/neo`.
- Review the files in [Changed files](#changed-files) to make sure the modifications still work with the new upstream code.
- Never run `git push --tags` or `git push --mirror`: the local clone holds upstream's tags and history, and those commands would upload them to this repository.

<br>

## License

This repository keeps the upstream [GNU Affero General Public License v3.0](LICENSE).

- `LICENSE` retains the upstream copyright notices: Copyright (c) 2023 AUTOMATIC1111, Copyright (c) 2024 lllyasviel, Copyright (c) 2025 Haoming02.
- The modifications in this repository are Copyright (c) 2026 sss22213 and are also released under AGPL-3.0. The [Modifications](#modifications) section serves as the notice of changes required by Section 5 of AGPL-3.0.
- AGPL-3.0 requires that if you run a modified version as a network service for others, you must offer them the corresponding source code.
- Third-party extensions are used under their own licenses. The NVIDIA CUDA container image is covered by the NVIDIA Deep Learning Container License.

<br>

<p align="center">
Thanks to <b>AUTOMATIC1111</b>, <b>lllyasviel</b>, <b>Haoming02</b>,<br>
and all upstream contributors and extension authors who made this project possible.
</p>
