"""Load Open WebUI's convert_output_to_messages (from the cloned repo) with
minimal stubs, so the reasoning-replay behavior can be tested without a full
Open WebUI install.

The cloned repo lives at /tmp/open-webui (see probes/litellm/README.md).
"""

import json
import sys
import types
from pathlib import Path

OWUI_BACKEND = Path("/tmp/open-webui/backend")


def _load_owui_misc() -> types.ModuleType:
    """Import open_webui.utils.misc with stubbed dependencies."""
    sys.path.insert(0, str(OWUI_BACKEND))

    # --- stub open_webui package chain -------------------------------
    open_webui = types.ModuleType("open_webui")
    open_webui.__path__ = []  # namespace package marker
    env = types.ModuleType("open_webui.env")
    env.CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE = 65536
    utils = types.ModuleType("open_webui.utils")
    utils.__path__ = []
    json_codec = types.ModuleType("open_webui.utils.json_codec")

    class _JSONCodec:
        @staticmethod
        def dumps(obj, *args, **kwargs):
            kwargs.pop("ensure_ascii", None)
            return json.dumps(obj, *args, **kwargs)

        @staticmethod
        def loads(s, *args, **kwargs):
            return json.loads(s, *args, **kwargs)

    json_codec.JSONCodec = _JSONCodec
    sys.modules["open_webui"] = open_webui
    sys.modules["open_webui.env"] = env
    sys.modules["open_webui.utils"] = utils
    sys.modules["open_webui.utils.json_codec"] = json_codec

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "owui_misc", OWUI_BACKEND / "open_webui" / "utils" / "misc.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["owui_misc"] = mod
    spec.loader.exec_module(mod)
    return mod
