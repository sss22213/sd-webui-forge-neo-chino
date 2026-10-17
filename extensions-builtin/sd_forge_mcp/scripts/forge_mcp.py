"""MCP server of the WebUI at /mcp (Model Context Protocol, Streamable HTTP).

AI agents (Claude Code / Desktop, Codex, the webui_gen_image app, ...) connect to
http://<host>:<port>/mcp with "Authorization: Bearer <token>" and get tools for txt2img,
img2img, the model / sampler / LoRA lists, PNG info, progress, memory and refresh.
Settings -> MCP Server turns it on or off and holds the token (generated on first start;
the environment variable FORGE_MCP_TOKEN overrides it). Needs the --api flag.
"""
import os
import secrets

from modules import script_callbacks, shared

from lib_mcp import a1111_tools
from lib_mcp.server import McpServer

VERSION = "1.0.0"
PATH = "/mcp"
SECTION = ("mcp_server", "MCP Server")
INSTRUCTIONS = (
    "Stable Diffusion WebUI (Forge Neo). Generate images with txt2img / img2img; anything you do not pass "
    "comes from the WebUI's own settings. Write prompts in the style of the loaded checkpoint (list_checkpoints "
    "shows it): danbooru-style tags for SDXL / Illustrious / Pony anime models. Use LoRAs as <lora:name:weight> "
    "in the prompt (list_loras)."
)


def _token() -> str:
    return os.environ.get("FORGE_MCP_TOKEN") or shared.opts.data.get("mcp_server_token") or ""


def _enabled() -> bool:
    return bool(shared.opts.data.get("mcp_server_enable", True))


def _ensure_token():
    if not os.environ.get("FORGE_MCP_TOKEN") and not shared.opts.data.get("mcp_server_token"):
        shared.opts.data["mcp_server_token"] = secrets.token_urlsafe(24)
        shared.opts.save(shared.config_filename)


def on_ui_settings():
    # before the Settings page is built, so it shows the generated token right away
    _ensure_token()
    shared.opts.add_option("mcp_server_enable", shared.OptionInfo(
        True, f"Serve the MCP endpoint {PATH} (Model Context Protocol, for AI agents)",
        section=SECTION, category_id="system").info("needs --api"))
    shared.opts.add_option("mcp_server_token", shared.OptionInfo(
        "", "Access token (clients send Authorization: Bearer <token>)",
        section=SECTION, category_id="system").info("generated at startup when empty; FORGE_MCP_TOKEN overrides it"))


def on_app_started(demo, app):
    _ensure_token()  # also without the UI (--nowebui)
    server = McpServer("forge-neo", VERSION, instructions=INSTRUCTIONS, get_token=_token, is_enabled=_enabled,
                       log=lambda m: print(f"[MCP] {m}"))
    a1111_tools.register(server, app)
    server.mount(app, PATH)
    print(f"[MCP] server at {PATH} ({len(server.tools)} tools); token in Settings -> MCP Server")


script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_app_started(on_app_started)
