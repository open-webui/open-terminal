import os
from pathlib import Path

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-token")

from fastapi.testclient import TestClient

from open_terminal.main import app


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _client() -> TestClient:
    return TestClient(app)


def _operation_ids(client: TestClient) -> set[str]:
    schema = client.get("/openapi.json").json()
    operation_ids: set[str] = set()
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                operation_ids.add(operation["operationId"])
    return operation_ids


def _operations_by_id(client: TestClient) -> dict[str, dict]:
    schema = client.get("/openapi.json").json()
    operations: dict[str, dict] = {}
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                operations[operation["operationId"]] = operation
    return operations


def _parameter_description(operation: dict, name: str) -> str:
    for parameter in operation.get("parameters", []):
        if parameter.get("name") == name:
            return parameter.get("description") or ""
    return ""


def test_openapi_exposes_only_core_model_tools():
    client = _client()

    assert _operation_ids(client) == {
        "get_environment",
        "run_command",
        "get_process_status",
        "list_processes",
        "send_process_input",
        "kill_process",
        "read_file",
        "write_file",
        "apply_patch",
        "display_file",
    }


def test_model_visible_tool_descriptions_are_explicit():
    client = _client()
    schema = client.get("/openapi.json").json()
    operations = _operations_by_id(client)

    for operation in operations.values():
        description = operation["description"]
        assert "Use when:" in description
        assert "Inputs:" in description
        assert "Returns:" in description
        assert "Errors:" in description

    environment_description = operations["get_environment"]["description"].lower()
    assert "before choosing os-specific commands" in environment_description
    assert "cli_versions" in environment_description

    read_description = operations["read_file"]["description"].lower()
    assert "large text files" in read_description
    assert "start_line" in read_description
    assert "raw http binary data" in read_description

    run_description = operations["run_command"]["description"].lower()
    assert "poll get_process_status" in run_description
    assert "process_id" in run_description
    assert "relative paths resolve against the session cwd" in run_description
    assert "get_environment" in run_description
    assert "this system is running" not in run_description

    write_description = operations["write_file"]["description"].lower()
    assert "409" in write_description
    assert "overwrite=true" in write_description
    assert "whole file" in write_description
    write_schema = schema["components"]["schemas"]["WriteRequest"]["properties"]
    assert "defaults to false" in write_schema["overwrite"]["description"].lower()

    assert "*** Begin Patch" in operations["apply_patch"]["description"]
    assert "*** Update File:" in operations["apply_patch"]["description"]
    assert "dry_run=true" in operations["apply_patch"]["description"]
    assert "on 409" in operations["apply_patch"]["description"].lower()
    assert "read_file" in operations["apply_patch"]["description"]
    patch_schema = schema["components"]["schemas"]["ApplyPatchRequest"]["properties"]
    assert "*** End Patch" in patch_schema["patch"]["description"]

    list_description = operations["list_processes"]["description"].lower()
    assert "tracked commands" in list_description
    assert "running, done, or killed" in list_description

    assert "process_id returned by run_command" in _parameter_description(
        operations["get_process_status"], "process_id"
    )
    assert "process_id returned by run_command" in _parameter_description(
        operations["send_process_input"], "process_id"
    )
    assert "process_id returned by run_command" in _parameter_description(
        operations["kill_process"], "process_id"
    )
    assert "literal escape sequences" in operations["send_process_input"]["description"].lower()


def test_model_visible_tools_publish_response_shapes():
    client = _client()
    schema = client.get("/openapi.json").json()
    operations = _operations_by_id(client)

    def response_ref(tool: str, status: str = "200") -> str:
        response_schema = operations[tool]["responses"][status]["content"]["application/json"]["schema"]
        return response_schema["$ref"].rsplit("/", 1)[-1]

    environment = schema["components"]["schemas"][response_ref("get_environment")]
    for field in [
        "os",
        "hostname",
        "user",
        "home",
        "cwd",
        "shell",
        "environment",
        "cli_versions",
        "permissions",
        "info",
    ]:
        assert field in environment["properties"]

    read_response = operations["read_file"]["responses"]["200"]
    assert "application/json" in read_response["content"]
    assert "image/png" in read_response["content"]
    assert "image/jpeg" in read_response["content"]
    assert "Raw binary" in read_response["description"]

    command_response = schema["components"]["schemas"][response_ref("run_command")]
    for field in ["id", "command", "status", "exit_code", "output", "truncated", "next_offset", "log_path"]:
        assert field in command_response["properties"]

    status_response = schema["components"]["schemas"][response_ref("get_process_status")]
    assert status_response["properties"].keys() == command_response["properties"].keys()

    apply_response = schema["components"]["schemas"][response_ref("apply_patch")]
    for field in ["applied", "dry_run", "changes", "conflicts"]:
        assert field in apply_response["properties"]
    apply_conflict_ref = operations["apply_patch"]["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    assert "detail" in schema["components"]["schemas"][apply_conflict_ref]["properties"]
    write_conflict_ref = operations["write_file"]["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    assert schema["components"]["schemas"][write_conflict_ref]["properties"]["detail"]["type"] == "string"
    patch_schema = schema["components"]["schemas"]["ApplyPatchRequest"]["properties"]["patch"]
    assert "*** Begin Patch" in str(patch_schema.get("examples"))


def test_get_environment_returns_runtime_and_cli_contract():
    client = _client()

    response = client.get("/environment", headers=AUTH_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["os"]["system"]
    assert data["hostname"]
    assert data["user"]
    assert data["home"]
    assert data["cwd"]
    assert data["shell"]
    assert "PATH" in data["environment"]
    for name in [
        "rg",
        "git",
        "jq",
        "python3",
        "node",
        "curl",
        "tar",
        "zip",
        "unzip",
        "find",
        "sed",
        "awk",
        "file",
        "patch",
        "diff",
    ]:
        assert name in data["cli_versions"]
        assert "available" in data["cli_versions"][name]


def test_write_file_does_not_overwrite_by_default(tmp_path: Path):
    client = _client()
    target = tmp_path / "note.txt"

    first = client.post(
        "/files/write",
        headers=AUTH_HEADERS,
        json={"path": str(target), "content": "first"},
    )
    second = client.post(
        "/files/write",
        headers=AUTH_HEADERS,
        json={"path": str(target), "content": "second"},
    )
    overwrite = client.post(
        "/files/write",
        headers=AUTH_HEADERS,
        json={"path": str(target), "content": "second", "overwrite": True},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    conflict_detail = second.json()["detail"]
    assert "overwrite=false" in conflict_detail
    assert "overwrite=true" in conflict_detail
    assert "apply_patch" in conflict_detail
    assert overwrite.status_code == 200
    assert target.read_text() == "second"


def test_apply_patch_supports_multi_hunk_dry_run_and_apply(tmp_path: Path):
    client = _client()
    target = tmp_path / "demo.txt"
    target.write_text("alpha\nbeta\ngamma\n")
    patch = f"""*** Begin Patch
*** Update File: {target}
@@
-alpha
+ALPHA
@@
-gamma
+GAMMA
*** End Patch"""

    dry_run = client.post(
        "/files/apply_patch",
        headers=AUTH_HEADERS,
        json={"patch": patch, "dry_run": True},
    )

    assert dry_run.status_code == 200
    assert dry_run.json()["applied"] is False
    assert dry_run.json()["dry_run"] is True
    assert target.read_text() == "alpha\nbeta\ngamma\n"

    applied = client.post(
        "/files/apply_patch",
        headers=AUTH_HEADERS,
        json={"patch": patch},
    )

    assert applied.status_code == 200
    assert applied.json()["applied"] is True
    assert target.read_text() == "ALPHA\nbeta\nGAMMA\n"


def test_apply_patch_reports_conflicts(tmp_path: Path):
    client = _client()
    target = tmp_path / "demo.txt"
    target.write_text("alpha\n")
    patch = f"""*** Begin Patch
*** Update File: {target}
@@
-missing
+present
*** End Patch"""

    response = client.post(
        "/files/apply_patch",
        headers=AUTH_HEADERS,
        json={"patch": patch},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Patch could not be applied" in detail["message"]
    assert "read_file" in detail["message"]
    assert "retry" in detail["message"]
    assert detail["conflicts"][0]["path"] == str(target)
    assert detail["conflicts"][0]["reason"] == "old content not found"
    assert target.read_text() == "alpha\n"


def test_full_dockerfile_installs_required_cli_contract():
    dockerfile = Path("Dockerfile").read_text()

    for package in ["ripgrep", "git", "jq", "nodejs", "curl", "zip", "unzip", "tar", "findutils", "sed", "gawk", "file", "patch", "diffutils"]:
        assert package in dockerfile
