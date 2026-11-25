"""Compatibility entry point; packaged builds use backend.app.egress_proxy_entrypoint."""
from backend.app.egress_proxy_process import main

if __name__ == "__main__":
    raise SystemExit(main())
