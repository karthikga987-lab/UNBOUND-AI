import base64
import hashlib
import struct
import zlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.browser.tools import BrowserToolAdapter
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.schemas.contracts import EvidenceItem, EvidenceSourceType, ToolResult


class _Controller:
    provider_name = "browserless_cloud_demo"

    def __init__(self, url: str, *, title: str = "DOE Vehicles", visible_text: str = "Vehicle technologies and programs"):
        self.current_url = url
        self._title = title
        self._visible_text = visible_text

    def title(self) -> str:
        return self._title

    def visible_content(self) -> str:
        return self._visible_text


class _SessionManager:
    def __init__(self, controller: _Controller):
        self._session = SimpleNamespace(controller=controller)

    def get_session(self, _task_id):
        return self._session


def _png_1x1() -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


def _adapter_and_result(tmp_path, *, final_url: str, requested_url: str, include_screenshot: bool = True, visible_text: str = "Vehicle technologies and programs"):
    task_id = uuid4()
    controller = _Controller(final_url, visible_text=visible_text)
    adapter = BrowserToolAdapter(
        workflow_store=WorkflowStore(tmp_path / "workflows"),
        session_manager=_SessionManager(controller),
    )
    adapter._verification_context[task_id] = {
        "operation": "navigate",
        "requested_url": requested_url,
        "provider": "browserless_cloud_demo",
    }
    observation = adapter._observe(controller)
    evidence = []
    if include_screenshot:
        image = _png_1x1()
        digest = hashlib.sha256(image).hexdigest()
        observation.update({
            "screenshot_format": "image/png",
            "screenshot_sha256": digest,
            "screenshot_base64": base64.b64encode(image).decode("ascii"),
        })
        evidence.append(EvidenceItem(
            task_id=task_id,
            source_type=EvidenceSourceType.BROWSER,
            source_name="Browserless Cloud PNG screenshot",
            source_url=final_url,
            excerpt=f"PNG SHA-256: {digest}",
            supports_claim="A bounded screenshot was captured from the observed browser page.",
        ))
    result = ToolResult(
        success=True,
        data={"task_id": str(task_id), "navigated": True, "page_observation": observation},
        evidence=evidence,
    )
    return adapter, task_id, result


def test_browserless_navigation_accepts_same_origin_redirect_and_verified_screenshot(tmp_path):
    requested = "https://www.energy.gov/eere/vehicles"
    final = "https://www.energy.gov/cmei/vehicles/transportation-technologies-office"
    adapter, _task_id, result = _adapter_and_result(
        tmp_path,
        requested_url=requested,
        final_url=final,
    )

    assert adapter.verify_result("browser.navigate", "The DOE page is observed", result) is True


@pytest.mark.parametrize("incomplete", ["missing_observation", "missing_visible_text", "missing_screenshot"])
def test_browserless_navigation_rejects_incomplete_observation(tmp_path, incomplete):
    adapter, _task_id, result = _adapter_and_result(
        tmp_path,
        requested_url="https://www.energy.gov/eere/vehicles",
        final_url="https://www.energy.gov/eere/vehicles",
        include_screenshot=incomplete != "missing_screenshot",
    )
    if incomplete == "missing_observation":
        result.data.pop("page_observation")
    elif incomplete == "missing_visible_text":
        result.data["page_observation"].pop("visible_text")

    assert adapter.verify_result("browser.navigate", "The DOE page is observed", result) is False


def test_browserless_navigation_rejects_empty_visible_text(tmp_path):
    adapter, _task_id, result = _adapter_and_result(
        tmp_path,
        requested_url="https://www.energy.gov/eere/vehicles",
        final_url="https://www.energy.gov/eere/vehicles",
        visible_text="",
    )

    assert adapter.verify_result("browser.navigate", "The DOE page is observed", result) is False


def test_browserless_navigation_rejects_cross_origin_redirect(tmp_path):
    adapter, _task_id, result = _adapter_and_result(
        tmp_path,
        requested_url="https://www.energy.gov/eere/vehicles",
        final_url="https://www.example.net/vehicles",
    )

    assert adapter.verify_result("browser.navigate", "The DOE page is observed", result) is False
