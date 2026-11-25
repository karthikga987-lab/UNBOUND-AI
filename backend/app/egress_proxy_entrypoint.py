"""PyInstaller entry point for unbound-egress-proxy.exe."""
from backend.app.egress_proxy_process import main


if __name__ == "__main__":
    raise SystemExit(main())
