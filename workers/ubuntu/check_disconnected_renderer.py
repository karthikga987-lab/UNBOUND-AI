"""Guest-console acceptance check. Run as the broker UID with the virtual cable off."""
import os
import stat
from uuid import uuid4

from backend.app.browser.worker_ipc import BrokerClient, RenderRequest, SOCKET
from backend.app.egress_proxy import LOCAL_DEMO_PRODUCT_URL


def main():
    client = BrokerClient()
    info = SOCKET.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != client.peer or stat.S_IMODE(info.st_mode) != 0o660:
        raise SystemExit("Unexpected renderer socket owner, type or access mode")
    if info.st_gid not in {os.getegid(), *os.getgroups()}:
        raise SystemExit("Broker is outside the IPC group")
    task, session = uuid4(), uuid4()

    def run(operation, **params):
        request = RenderRequest(task_id=task, session_id=session, operation={"operation": operation, **params})
        result = client.execute(request)
        if not result.success:
            raise SystemExit(f"Renderer check failed: {result.failure_code}")
        return result

    try:
        if run("navigate", url=LOCAL_DEMO_PRODUCT_URL).current_url != LOCAL_DEMO_PRODUCT_URL:
            raise SystemExit("Fixture navigation mismatch")
        clicked = run("click", target={"type": "role", "value": "link", "name": "View details", "exact": True})
        observed = run("extract_visible", max_chars=1024)
        if (clicked.current_url != "http://unbound-demo.invalid/details"
                or observed.current_url != clicked.current_url or "synthetic" not in (observed.visible_text or "")):
            raise SystemExit("Fixture observation mismatch")
        if not run("screenshot").screenshot_base64:
            raise SystemExit("Missing fixture screenshot")
    finally:
        run("close")
    print("Completed renderer IPC, fixture navigation, click, observation, screenshot and close.")


if __name__ == "__main__":
    main()
