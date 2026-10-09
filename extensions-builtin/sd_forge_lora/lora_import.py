"""LoRA import API (sd-webui-forge-neo-chino).

POST /sdapi/v1/lora/import   JSON: download a .safetensors from a URL into the Lora folder
POST /sdapi/v1/lora/upload   multipart: same, with the file in the request
GET  /sdapi/v1/lora/import   check that the API is available, and where files go

Both POST endpoints:
- check that the file really is a safetensors file (the header is parsed; nothing is executed)
- never write outside the Lora folder (file and sub folder names are sanitized)
- keep an identical existing file ("exists"), refuse a different one with the same name unless overwrite=true (HTTP 409)
- write the card metadata Forge reads from <name>.json (activation text, preferred weight, sd version, ...)
  and an optional preview image <name>.preview.<ext>
- register only the new file, so it shows up in the Lora tab without a restart

This module only depends on the standard library, FastAPI and requests; Forge passes in the Lora folder and
the refresh function (see scripts/lora_script.py), which keeps it testable outside Forge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import threading
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

MAX_LORA_BYTES = 4 * 1024**3  # largest file accepted
MAX_HEADER_BYTES = 100 * 1024**2  # largest safetensors JSON header accepted
MAX_PREVIEW_BYTES = 20 * 1024**2
PREVIEW_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "video/mp4": "mp4", "video/webm": "webm"}
CHUNK = 1024 * 1024
_name_ok = re.compile(r"[^\w.\- ()\[\]+]", re.UNICODE)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class LoraImportBase(BaseModel):
    filename: Optional[str] = Field(None, description="File name to save as (.safetensors is added). Default: taken from the URL / upload")
    subfolder: str = Field("", description="Sub folder inside the Lora folder, e.g. 'LoRA-Tag-Studio' ('' = the Lora folder itself)")
    activation_text: str = Field("", description="Added to the prompt after <lora:...> when the card is clicked (e.g. the trigger word)")
    preferred_weight: float = Field(0.0, description="Weight used when the card is clicked (0 = the default multiplier)")
    negative_text: str = ""
    description: str = ""
    notes: str = ""
    sd_version: str = Field("Unknown", description="Architecture for the preset filter: sd, xl, flux, klein, anima, ... or Unknown")
    preview_url: Optional[str] = Field(None, description="Image (or mp4 / webm) to show on the card")
    overwrite: bool = Field(False, description="Replace an existing file with the same name but different content")
    sha256: Optional[str] = Field(None, description="Expected SHA-256 of the file; the import fails if it differs")


class LoraImportRequest(LoraImportBase):
    url: str = Field(..., description="http(s) URL of the .safetensors file")


def _clean_name(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _name_ok.sub("_", base).strip(" .")
    if base.lower().endswith(".safetensors"):
        base = base[: -len(".safetensors")].strip(" .")
    if not base:
        raise HTTPException(400, "filename is empty after removing unsupported characters")
    return base[:150] + ".safetensors"


def _clean_subfolder(sub: str) -> str:
    parts = [p for p in str(sub or "").replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(400, "subfolder cannot contain '..'")
    return os.path.join(*[_name_ok.sub("_", p).strip(" .") or "_" for p in parts]) if parts else ""


def _check_url(url: str, what: str) -> None:
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(400, f"{what} must be an http(s) URL")


def _check_safetensors(path: str) -> int:
    """Parse the safetensors header without loading tensors; returns the number of tensors."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            raise HTTPException(400, "not a safetensors file (too short)")
        (n,) = struct.unpack("<Q", head)
        if n <= 1 or n > MAX_HEADER_BYTES or 8 + n > size:
            raise HTTPException(400, "not a safetensors file (bad header length)")
        try:
            header = json.loads(f.read(n))
        except ValueError as e:
            raise HTTPException(400, "not a safetensors file (header is not JSON)") from e
    if not isinstance(header, dict):
        raise HTTPException(400, "not a safetensors file (header is not an object)")
    tensors = [k for k in header if k != "__metadata__"]
    if not tensors:
        raise HTTPException(400, "the safetensors file contains no tensors")
    return len(tensors)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _lock_for(path: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def _download(url: str, dest_dir: str, limit: int, timeout: float = 60) -> tuple[str, str, str]:
    """Stream a URL into a temp file in dest_dir; returns (temp path, sha256, content type)."""
    fd, tmp = tempfile.mkstemp(prefix=".import-", suffix=".part", dir=dest_dir)
    h, total = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as out, requests.get(url, stream=True, timeout=(15, timeout), allow_redirects=True) as r:
            if r.status_code != 200:
                raise HTTPException(502, f"download failed: HTTP {r.status_code}")
            length = int(r.headers.get("content-length") or 0)
            if length > limit:
                raise HTTPException(413, f"file too large ({length} bytes, limit {limit})")
            for block in r.iter_content(CHUNK):
                total += len(block)
                if total > limit:
                    raise HTTPException(413, f"file too large (limit {limit} bytes)")
                h.update(block)
                out.write(block)
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    except requests.RequestException as e:
        os.unlink(tmp)
        raise HTTPException(502, f"download failed: {e}") from e
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp, h.hexdigest(), ctype


def _user_metadata(req: LoraImportBase, sd_versions: list[str]) -> dict:
    sd_version = req.sd_version if req.sd_version in sd_versions else "Unknown"
    return {
        "description": req.description,
        "sd version": sd_version,
        "activation text": req.activation_text,
        "preferred weight": float(req.preferred_weight or 0.0),
        "negative text": req.negative_text,
        "notes": req.notes,
    }


def _save_preview(url: str, stem_path: str) -> Optional[str]:
    """Best effort: a missing preview never fails the import."""
    try:
        _check_url(url, "preview_url")
        tmp, _, ctype = _download(url, os.path.dirname(stem_path), MAX_PREVIEW_BYTES, timeout=30)
    except HTTPException:
        return None
    ext = PREVIEW_TYPES.get(ctype) or os.path.splitext(urlparse(url).path)[1].lstrip(".").lower()
    if ext not in PREVIEW_TYPES.values() and ext != "jpeg":
        os.unlink(tmp)
        return None
    for old in PREVIEW_TYPES.values():  # one preview per LoRA
        if os.path.exists(f"{stem_path}.preview.{old}"):
            os.unlink(f"{stem_path}.preview.{old}")
    dest = f"{stem_path}.preview.{ext}"
    os.replace(tmp, dest)
    return dest


def _finish(tmp: str, sha: str, req: LoraImportBase, name: Optional[str], lora_dir: str,
            refresh: Callable[[str], Optional[dict]], sd_versions: list[str]) -> dict:
    """Validate the downloaded / uploaded file and move it into place."""
    try:
        tensors = _check_safetensors(tmp)
        if req.sha256 and req.sha256.lower() != sha:
            raise HTTPException(400, f"sha256 mismatch: expected {req.sha256.lower()}, got {sha}")
    except BaseException:
        os.unlink(tmp)
        raise
    filename = _clean_name(req.filename or name or f"lora_{sha[:10]}")
    folder = os.path.join(lora_dir, _clean_subfolder(req.subfolder))
    dest = os.path.join(folder, filename)
    with _lock_for(dest):
        status = "imported"
        if os.path.exists(dest):
            if _sha256(dest) == sha:
                status = "exists"
            elif not req.overwrite:
                os.unlink(tmp)
                raise HTTPException(409, f"{os.path.relpath(dest, lora_dir)} already exists with different content; set overwrite=true to replace it")
            else:
                status = "replaced"
        if status == "exists":
            os.unlink(tmp)
        else:
            os.replace(tmp, dest)
        stem_path = dest[: -len(".safetensors")]
        meta = _user_metadata(req, sd_versions)
        if any(v not in ("", 0.0, "Unknown") for v in meta.values()):
            with open(f"{stem_path}.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=4)
        preview = _save_preview(req.preview_url, stem_path) if req.preview_url else None
    info = refresh(os.path.splitext(filename)[0]) or {}
    alias = info.get("alias") or os.path.splitext(filename)[0]
    weight = meta["preferred weight"] or 1.0
    prompt = f"<lora:{alias}:{weight:g}>" + (f" {meta['activation text']}" if meta["activation text"] else "")
    return {
        "status": status,
        "name": info.get("name") or os.path.splitext(filename)[0],
        "alias": alias,
        "path": dest,
        "relative_path": os.path.relpath(dest, lora_dir),
        "size": os.path.getsize(dest),
        "sha256": sha,
        "tensors": tensors,
        "preview": preview,
        "prompt": prompt,
    }


def register(app: FastAPI, lora_dir: Callable[[], str], refresh: Callable[[str], Optional[dict]], sd_versions: list[str]) -> None:
    """lora_dir() → the Lora folder; refresh(name) registers one new file and returns {name, alias} (or None)."""

    def target_dir(sub: str) -> str:
        folder = os.path.join(lora_dir(), _clean_subfolder(sub))
        os.makedirs(folder, exist_ok=True)
        return folder

    @app.get("/sdapi/v1/lora/import")
    def lora_import_info():
        root = lora_dir()
        subs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))) if os.path.isdir(root) else []
        return {"version": 1, "lora_dir": root, "subfolders": subs, "sd_versions": sd_versions,
                "max_bytes": MAX_LORA_BYTES, "free_bytes": shutil.disk_usage(root).free if os.path.isdir(root) else None}

    @app.post("/sdapi/v1/lora/import")
    def lora_import(req: LoraImportRequest):
        _check_url(req.url, "url")
        tmp, sha, _ = _download(req.url, target_dir(req.subfolder), MAX_LORA_BYTES, timeout=120)
        return _finish(tmp, sha, req, os.path.basename(urlparse(req.url).path), lora_dir(), refresh, sd_versions)

    @app.post("/sdapi/v1/lora/upload")
    def lora_upload(
        file: UploadFile = File(...),
        filename: Optional[str] = Form(None),
        subfolder: str = Form(""),
        activation_text: str = Form(""),
        preferred_weight: float = Form(0.0),
        negative_text: str = Form(""),
        description: str = Form(""),
        notes: str = Form(""),
        sd_version: str = Form("Unknown"),
        preview_url: Optional[str] = Form(None),
        overwrite: bool = Form(False),
        sha256: Optional[str] = Form(None),
    ):
        req = LoraImportBase(filename=filename, subfolder=subfolder, activation_text=activation_text, preferred_weight=preferred_weight,
                             negative_text=negative_text, description=description, notes=notes, sd_version=sd_version,
                             preview_url=preview_url, overwrite=overwrite, sha256=sha256)
        fd, tmp = tempfile.mkstemp(prefix=".import-", suffix=".part", dir=target_dir(subfolder))
        h, total = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out:
                for block in iter(lambda: file.file.read(CHUNK), b""):
                    total += len(block)
                    if total > MAX_LORA_BYTES:
                        raise HTTPException(413, f"file too large (limit {MAX_LORA_BYTES} bytes)")
                    h.update(block)
                    out.write(block)
        except BaseException:
            os.unlink(tmp)
            raise
        return _finish(tmp, h.hexdigest(), req, file.filename, lora_dir(), refresh, sd_versions)
