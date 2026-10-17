"""MCP tools of the WebUI: thin adapters over its own HTTP API (/sdapi/v1/...).

Every tool calls the API in-process (httpx ASGITransport on the WebUI's FastAPI app), so it
behaves exactly like the REST API, needs no network, and the WebUI must be started with --api.
Image generation streams progress notifications while it runs.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .server import Context, McpServer, Result, ToolError, image, text

_app: Any = None

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

_GEN_PROPS: dict[str, dict] = {
    "prompt": {"type": "string", "description": "Positive prompt (LoRA tags like <lora:file:0.8> work here)"},
    "negative_prompt": {"type": "string"},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
    "steps": {"type": "integer"},
    "cfg_scale": {"type": "number"},
    "sampler_name": {"type": "string", "description": "See list_samplers"},
    "scheduler": {"type": "string", "description": "See list_schedulers"},
    "seed": {"type": "integer", "description": "-1 = random"},
    "batch_size": {"type": "integer"},
    "n_iter": {"type": "integer", "description": "Number of batches"},
    "checkpoint": {"type": "string", "description": "Checkpoint to use for this request only (name or title from list_checkpoints); the previous one is restored afterwards"},
    "styles": {"type": "array", "items": {"type": "string"}, "description": "Saved prompt styles to apply (see list_styles)"},
    "override_settings": {"type": "object", "description": "WebUI settings for this request only, e.g. {\"CLIP_stop_at_last_layers\": 2}"},
    "save_images": {"type": "boolean", "description": "Also save the images into the WebUI output folder (default false)"},
    "image_format": {"type": "string", "enum": ["original", "jpeg"],
                     "description": "original (default: as the WebUI encodes them, PNG with metadata) or jpeg (much smaller results)"},
    "extra": {"type": "object", "description": "Any other field of the /sdapi/v1/txt2img (or img2img) request, e.g. alwayson_scripts; merged into the request as is"},
}

_TXT2IMG_PROPS = {
    **_GEN_PROPS,
    "enable_hr": {"type": "boolean", "description": "Hires. fix"},
    "hr_scale": {"type": "number"},
    "hr_upscaler": {"type": "string", "description": "See list_upscalers"},
    "hr_second_pass_steps": {"type": "integer"},
    "denoising_strength": {"type": "number", "description": "Hires. fix denoising strength"},
}

_IMG2IMG_PROPS = {
    **_GEN_PROPS,
    "init_images": {"type": "array", "items": {"type": "string"}, "description": "Source image(s) as base64 or data URLs"},
    "mask": {"type": "string", "description": "Inpainting mask as base64 or data URL (white = repaint)"},
    "denoising_strength": {"type": "number", "description": "0 = keep the image, 1 = ignore it (default 0.75)"},
    "resize_mode": {"type": "integer", "description": "0 just resize, 1 crop and resize, 2 resize and fill, 3 latent upscale"},
    "inpainting_fill": {"type": "integer", "description": "0 fill, 1 original, 2 latent noise, 3 latent nothing"},
    "mask_blur": {"type": "integer"},
    "inpaint_full_res": {"type": "boolean", "description": "Inpaint only the masked area at full resolution"},
}


def _basic_auth():
    """The WebUI's --api-auth credentials, so in-process calls pass the API's auth check."""
    try:
        from modules import shared
    except ImportError:  # outside the WebUI (tests)
        return None
    pair = (getattr(shared.cmd_opts, "api_auth", None) or "").split(",")[0]
    user, sep, password = pair.partition(":")
    return httpx.BasicAuth(user, password) if sep else None


async def _api(method: str, path: str, *, params: dict | None = None, body: Any = None, timeout: float | None = 60) -> Any:
    if _app is None:
        raise ToolError("The WebUI has not finished starting")
    transport = httpx.ASGITransport(app=_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://webui.local", timeout=timeout,
                                 auth=_basic_auth()) as client:
        resp = await client.request(method, path, params=params, json=body)
    if resp.status_code == 404 and path.startswith("/sdapi/"):
        raise ToolError("The WebUI API is off: start the WebUI with --api")
    if resp.status_code >= 400:
        raise ToolError(f"{method} {path} failed: HTTP {resp.status_code} {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _mime(b64: str) -> str:
    if b64.startswith("/9j/"):
        return "image/jpeg"
    if b64.startswith("UklGR"):
        return "image/webp"
    return "image/png"


def _b64(value: str) -> str:
    return value.split(",", 1)[1] if value.startswith("data:") else value


def _jpeg(b64: str) -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _payload(args: dict) -> dict:
    payload = {k: v for k, v in args.items()
               if k not in ("checkpoint", "extra", "save_images", "image_format") and v is not None}
    if args.get("checkpoint"):
        payload["override_settings"] = {**(payload.get("override_settings") or {}),
                                        "sd_model_checkpoint": args["checkpoint"]}
    payload["save_images"] = bool(args.get("save_images"))
    payload["send_images"] = True
    payload.update(args.get("extra") or {})
    return payload


async def _generate(kind: str, payload: dict, ctx: Context, image_format: str | None = None) -> Result:
    job = asyncio.ensure_future(_api("POST", f"/sdapi/v1/{kind}", body=payload, timeout=None))
    while True:
        done, _ = await asyncio.wait({job}, timeout=1.0)
        if done:
            break
        try:
            p = await _api("GET", "/sdapi/v1/progress", params={"skip_current_image": "true"}, timeout=10)
            state = p.get("state") or {}
            step, steps = state.get("sampling_step"), state.get("sampling_steps")
            msg = f"step {step}/{steps}" if steps else (state.get("job") or "waiting")
            await ctx.progress(round(float(p.get("progress") or 0) * 100, 1), 100, msg)
        except Exception:  # noqa: BLE001 — progress is best effort
            pass
    data = job.result()
    images = data.get("images") or []
    if image_format == "jpeg":
        images = [await asyncio.to_thread(_jpeg, b) for b in images]
    try:
        info = json.loads(data.get("info") or "{}")
    except ValueError:
        info = {"raw": data.get("info")}
    infotexts = info.get("infotexts") or []
    summary = f"Generated {len(images)} image(s)."
    if info.get("all_seeds"):
        summary += f" Seeds: {info['all_seeds']}."
    if infotexts:
        summary += f"\nInfotext of the first image:\n{infotexts[0]}"
    params = {k: v for k, v in (data.get("parameters") or {}).items() if k not in ("init_images", "mask")}
    content = [image(b, _mime(b)) for b in images] + [text(summary)]
    return Result(content, {"info": info, "parameters": params, "count": len(images)})


async def txt2img(args: dict, ctx: Context) -> Result:
    return await _generate("txt2img", _payload(args), ctx, args.get("image_format"))


async def img2img(args: dict, ctx: Context) -> Result:
    payload = _payload(args)
    payload["init_images"] = [_b64(i) for i in args.get("init_images") or []]
    if not payload["init_images"]:
        raise ToolError("init_images needs at least one image")
    if args.get("mask"):
        payload["mask"] = _b64(args["mask"])
    return await _generate("img2img", payload, ctx, args.get("image_format"))


def _ckpt_key(name: str) -> str:
    """Title "foo.safetensors [hash]", file name or model_name -> the name without hash and extension."""
    return re.sub(r"\.(safetensors|ckpt|gguf|pt|pth)$", "", (name or "").split(" [", 1)[0].strip(), flags=re.I)


async def list_checkpoints(args: dict, ctx: Context) -> dict:
    models = await _api("GET", "/sdapi/v1/sd-models")
    current = _ckpt_key((await _api("GET", "/sdapi/v1/options")).get("sd_model_checkpoint") or "")
    q = (args.get("q") or "").lower()
    out, loaded = [], current
    for m in models:
        rel = re.split(r"[\\/]Stable-diffusion[\\/]", m.get("filename") or "", maxsplit=1)[-1]
        if current and current in (_ckpt_key(m.get("title")), _ckpt_key(m.get("model_name")), _ckpt_key(rel)):
            loaded = m.get("model_name")
        if q and q not in (m.get("title") or "").lower():
            continue
        out.append({"name": m.get("model_name"), "title": m.get("title"), "file": rel, "hash": m.get("hash")})
    return {"current": loaded, "count": len(out), "checkpoints": out}


async def set_checkpoint(args: dict, ctx: Context) -> str:
    await ctx.progress(0, None, f"loading {args['checkpoint']}")
    await _api("POST", "/sdapi/v1/options", body={"sd_model_checkpoint": args["checkpoint"]}, timeout=None)
    current = (await _api("GET", "/sdapi/v1/options")).get("sd_model_checkpoint")
    return f"Loaded checkpoint: {current}"


def _lister(path: str, pick):
    async def run(args: dict, ctx: Context) -> Any:
        data = await _api("GET", path)
        items = pick(data)
        q = (args.get("q") or "").lower()
        if q:
            items = [i for i in items if q in json.dumps(i, ensure_ascii=False).lower()]
        return {"count": len(items), "items": items}
    return run


async def list_loras(args: dict, ctx: Context) -> dict:
    items = await _api("GET", "/sdapi/v1/loras")
    q = (args.get("q") or "").lower()
    if q:
        items = [x for x in items if q in f"{x.get('name')} {x.get('alias')}".lower()]
    if not args.get("full"):
        items = [{"name": x.get("name"), "alias": x.get("alias")} for x in items]
    return {"count": len(items), "items": items}


async def png_info(args: dict, ctx: Context) -> dict:
    data = await _api("POST", "/sdapi/v1/png-info", body={"image": args["image"]})
    return {"parameters": data.get("info") or "", "items": data.get("items") or {},
            "parsed": data.get("parameters") or {}}


async def get_progress(args: dict, ctx: Context) -> Result:
    preview = bool(args.get("preview"))
    p = await _api("GET", "/sdapi/v1/progress", params={"skip_current_image": "false" if preview else "true"})
    current = p.pop("current_image", None)
    state = p.get("state") or {}
    summary = (f"progress {round(float(p.get('progress') or 0) * 100)}%, step {state.get('sampling_step')}/"
               f"{state.get('sampling_steps')}, job {state.get('job') or '-'}, eta {p.get('eta_relative')}s")
    content = [text(summary)] + ([image(current, _mime(current))] if current else [])
    return Result(content, p)


def _post(path: str, done: str, params: list[str] | None = None, timeout: float | None = 120):
    async def run(args: dict, ctx: Context) -> Any:
        query = {k: str(args[k]).lower() if isinstance(args[k], bool) else args[k]
                 for k in params or [] if args.get(k) is not None}
        data = await _api("POST", path, params=query or None, timeout=timeout)
        return data if isinstance(data, (dict, list)) and data else done
    return run


def register(server: McpServer, app: Any) -> None:
    global _app
    _app = app
    add = server.add_tool
    add("txt2img", "Generate images from a text prompt with Stable Diffusion (the WebUI's own settings fill in "
        "anything not given). Returns the images plus the generation info; sends progress while it runs.",
        {"properties": _TXT2IMG_PROPS, "required": ["prompt"]}, txt2img)
    add("img2img", "Change existing image(s) with a prompt (img2img; with a mask it inpaints). Returns the new "
        "images plus the generation info; sends progress while it runs.",
        {"properties": _IMG2IMG_PROPS, "required": ["prompt", "init_images"]}, img2img)
    add("list_checkpoints", "List the Stable Diffusion checkpoints (models) installed in the WebUI and which one "
        "is loaded.", {"properties": {"q": {"type": "string", "description": "Optional substring filter"}}},
        list_checkpoints, READ_ONLY)
    add("set_checkpoint", "Load another checkpoint and keep it as the WebUI's current model (slow: reads the "
        "model file). To use a checkpoint for one image only, pass checkpoint to txt2img instead.",
        {"properties": {"checkpoint": {"type": "string", "description": "Name or title from list_checkpoints"}},
         "required": ["checkpoint"]}, set_checkpoint)
    q_only = {"properties": {"q": {"type": "string", "description": "Optional substring filter"}}}
    add("list_samplers", "List the sampler names txt2img/img2img accept.", q_only,
        _lister("/sdapi/v1/samplers", lambda d: [{"name": s.get("name"), "aliases": s.get("aliases")} for s in d]),
        READ_ONLY)
    add("list_schedulers", "List the scheduler names txt2img/img2img accept.", q_only,
        _lister("/sdapi/v1/schedulers", lambda d: [{"name": s.get("name"), "label": s.get("label")} for s in d]),
        READ_ONLY)
    add("list_upscalers", "List the upscaler names (for hires. fix).", q_only,
        _lister("/sdapi/v1/upscalers", lambda d: [u.get("name") for u in d]), READ_ONLY)
    add("list_loras", "List the LoRAs the WebUI has (use them in a prompt as <lora:name:weight>). full=true "
        "also returns each file's path and the training metadata stored in it (large).",
        {"properties": {"q": {"type": "string", "description": "Optional substring filter"},
                        "full": {"type": "boolean", "description": "Include path and training metadata"}}},
        list_loras, READ_ONLY)
    add("list_modules", "List the extra modules (VAE / text encoders) the WebUI can load next to a checkpoint "
        "(override_settings.forge_additional_modules).", q_only,
        _lister("/sdapi/v1/sd-modules", lambda d: [{"name": m.get("model_name"), "file": m.get("filename")}
                                                  for m in d]), READ_ONLY)
    add("list_embeddings", "List the loaded textual-inversion embeddings (use them by writing their name in a "
        "prompt).", q_only,
        _lister("/sdapi/v1/embeddings", lambda d: sorted((d.get("loaded") or {}).keys())), READ_ONLY)
    add("list_styles", "List the saved prompt styles.", q_only,
        _lister("/sdapi/v1/prompt-styles", lambda d: d), READ_ONLY)
    add("png_info", "Read the generation parameters (prompt, seed, sampler, model ...) stored in a PNG made by a "
        "Stable Diffusion WebUI.",
        {"properties": {"image": {"type": "string", "description": "The PNG as base64 or data URL"}},
         "required": ["image"]}, png_info, READ_ONLY)
    add("get_progress", "Progress of the image being generated right now (optionally with the current preview).",
        {"properties": {"preview": {"type": "boolean", "description": "Include the live preview image"}}},
        get_progress, READ_ONLY)
    add("interrupt", "Stop the image generation that is running now.", {},
        _post("/sdapi/v1/interrupt", "Interrupted."))
    add("skip", "Skip the current image of a batch and continue with the next.", {},
        _post("/sdapi/v1/skip", "Skipped."))
    add("refresh_checkpoints", "Make the WebUI re-scan its checkpoint folder so a newly downloaded checkpoint "
        "appears in the model list. Call it after a successful checkpoint download.", {},
        _post("/sdapi/v1/refresh-checkpoints", "Checkpoint list refreshed."))
    add("refresh_loras", "Make the WebUI re-scan its LoRA folder so a newly downloaded LoRA can be used "
        "immediately. Call it after a successful LoRA download.", {},
        _post("/sdapi/v1/refresh-loras", "LoRA list refreshed."))
    add("refresh_embeddings", "Make the WebUI re-scan its embeddings (textual inversion) folder. Call it after "
        "renaming an embedding.", {}, _post("/sdapi/v1/refresh-embeddings", "Embedding list refreshed."))
    add("a1111_memory", "Show memory usage of the WebUI: system RAM, the whole GPU's VRAM used/free, how much "
        "VRAM the WebUI itself holds, whether the checkpoint is currently on the GPU, and the out-of-memory count.",
        {}, _lister_raw("/sdapi/v1/memory"), READ_ONLY)
    add("a1111_unload_checkpoint", "Unload the checkpoint to free memory. full=false (default): free VRAM only, "
        "the model stays in system RAM so the next generation starts fastest. full=true: free both VRAM and RAM; "
        "the next generation re-reads the model file from disk automatically (slower). Returns the memory state "
        "afterwards.",
        {"properties": {"full": {"type": "boolean", "description": "true = also free system RAM"}}},
        _post("/sdapi/v1/unload-checkpoint", "Unloaded.", ["full"]))
    add("a1111_reload_checkpoint", "Load the checkpoint back into memory now (mainly after a full unload, so the "
        "next generation does not have to read the file from disk). On Forge the weights only move onto the GPU "
        "when the next image is generated. Returns the memory state afterwards.", {},
        _post("/sdapi/v1/reload-checkpoint", "Reloaded.", timeout=300))


def _lister_raw(path: str):
    async def run(args: dict, ctx: Context) -> Any:
        return await _api("GET", path)
    return run
