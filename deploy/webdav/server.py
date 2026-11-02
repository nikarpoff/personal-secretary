"""WsgiDAV configuration with credentials supplied through secrets/webdav.env."""

import os
import signal

from cheroot.wsgi import Server
from wsgidav.wsgidav_app import WsgiDAVApp


def main() -> None:
    username = os.environ.get("WEBDAV_USERNAME", "").strip()
    password = os.environ.get("WEBDAV_PASSWORD", "")
    if not username or not password.strip() or password.startswith("<"):
        raise SystemExit("Set WEBDAV_USERNAME and WEBDAV_PASSWORD in secrets/webdav.env")
    vault_name = os.environ.get("OBSIDIAN_VAULT_NAME", "Obsidian")
    if (
        not vault_name.strip()
        or vault_name != vault_name.strip()
        or any(c in vault_name for c in "/\\\x00\r\n")
        or vault_name in {".", ".."}
    ):
        raise SystemExit("OBSIDIAN_VAULT_NAME must be a single vault name")
    config = {
        "provider_mapping": {f"/{vault_name}": "/var/lib/dav"},
        "fs_dav_provider": {"follow_symlinks": False},
        "http_authenticator": {
            "domain_controller": None,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "simple_dc": {"user_mapping": {"*": {username: {"password": password}}}},
        "dir_browser": {"enable": False},
        "lock_storage": True,
        "property_manager": None,
        "verbose": 2,
    }
    server = Server(("0.0.0.0", 8080), WsgiDAVApp(config), numthreads=50)
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
