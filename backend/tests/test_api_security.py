import pytest
from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.schemas.contracts import TaskCreateRequest


def make_client(tmp_path, *, client=("127.0.0.1", 12345), raise_server_exceptions=True):
    runtime = TaskRuntime(conversation_store=SQLiteConversationStore(tmp_path / "security.sqlite3"))
    return TestClient(create_app(runtime), client=client, raise_server_exceptions=raise_server_exceptions)


def test_non_loopback_peer_is_rejected_before_api_access(tmp_path):
    client = make_client(tmp_path, client=("203.0.113.20", 12345))

    response = client.get("/api/health")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_only"


def test_untrusted_host_header_is_rejected(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/api/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_host"


def test_untrusted_browser_origin_cannot_mutate_local_state(tmp_path):
    client = make_client(tmp_path)

    response = client.post("/api/conversations", headers={"Origin": "https://attacker.example"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"


def test_allowed_voice_prototype_origin_can_create_a_session(tmp_path):
    client = make_client(tmp_path)

    response = client.post("/api/conversations", headers={"Origin": "http://127.0.0.1:8001"})

    assert response.status_code == 201


@pytest.mark.parametrize("origin", [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://127.0.0.1:5175",
    "http://127.0.0.1:5176",
    "http://127.0.0.1:5178",
])
def test_allowed_vite_dev_origins_can_create_a_session(tmp_path, origin):
    client = make_client(tmp_path)

    response = client.post("/api/conversations", headers={"Origin": origin})

    assert response.status_code == 201


@pytest.mark.parametrize("origin", [
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:5176",
    "https://localhost:5173",
])
def test_other_vite_origins_cannot_mutate_local_state(tmp_path, origin):
    client = make_client(tmp_path)

    response = client.post("/api/conversations", headers={"Origin": origin})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"


def test_browserless_origin_is_exact_and_cors_preflight_is_allowed(tmp_path):
    client = make_client(tmp_path)

    preflight = client.options(
        "/api/assistant/requests",
        headers={
            "Origin": "http://127.0.0.1:5178",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    accepted = client.post("/api/conversations", headers={"Origin": "http://127.0.0.1:5178"})
    alias = client.post("/api/conversations", headers={"Origin": "http://localhost:5178"})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5178"
    assert accepted.status_code == 201
    assert alias.status_code == 403
    assert alias.json()["detail"]["code"] == "origin_not_allowed"


def test_task_state_does_not_survive_runtime_recreation(tmp_path):
    database = tmp_path / "restart.sqlite3"
    before_restart = TaskRuntime(conversation_store=SQLiteConversationStore(database))
    task = before_restart.manager.create(TaskCreateRequest(user_request="Synthetic restart test"), [])

    after_restart = TaskRuntime(conversation_store=SQLiteConversationStore(database))

    try:
        after_restart.manager.get(task.task_id)
    except KeyError:
        pass
    else:
        raise AssertionError("Task state must remain process-local and must not be implicitly restored")


def test_oversized_request_body_is_rejected_before_route_validation(tmp_path):
    client = make_client(tmp_path)

    response = client.post("/api/conversation", content=b"x" * (1024 * 1024 + 1))

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_too_large"


def test_unhandled_error_logs_only_exception_class_not_private_details(tmp_path, caplog):
    client = make_client(tmp_path, raise_server_exceptions=False)
    app = client.app

    @app.get("/api/test-private-error")
    def private_error():
        raise RuntimeError("PRIVATE_TOKEN=example-private-value C:\\Users\\person\\secret.txt")

    response = client.get("/api/test-private-error")

    assert response.status_code == 500
    assert "example-private-value" not in response.text
    assert "secret.txt" not in response.text
    assert "example-private-value" not in caplog.text
    assert "secret.txt" not in caplog.text
    assert "exception_class=RuntimeError" in caplog.text
