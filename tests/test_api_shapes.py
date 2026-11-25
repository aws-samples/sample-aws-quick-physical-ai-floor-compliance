"""Offline checks of the AWS request bodies the scripts send, against botocore's
service models. Catches wrong parameter names and shapes without an account."""
import json
import re
import sys
from pathlib import Path

import boto3
import pytest
from botocore.validate import ParamValidator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import quicksight_defs as qd  # noqa: E402

# No credentials are needed: these tests only validate request shapes against the
# service models bundled with boto3 and never send a request.
SESSION = boto3.Session(region_name="us-east-1")


def _deep(shape, v, path: str, errs: list[str]) -> None:
    """botocore checks names and types only. This also checks what the service enforces and the
    model documents: string patterns, lengths, enums and numeric ranges, including map keys."""
    t, m = shape.type_name, shape.metadata
    if t == "structure" and isinstance(v, dict):
        for k, x in v.items():
            if k in shape.members:
                _deep(shape.members[k], x, f"{path}.{k}", errs)
    elif t == "list" and isinstance(v, list):
        if len(v) < m.get("min", 0) or len(v) > m.get("max", 10**9):
            errs.append(f"{path}: {len(v)} items, allowed {m.get('min')}..{m.get('max')}")
        for i, x in enumerate(v):
            _deep(shape.member, x, f"{path}[{i}]", errs)
    elif t == "map" and isinstance(v, dict):
        for k, x in v.items():
            _deep(shape.key, k, f"{path}<key>", errs)
            _deep(shape.value, x, f"{path}.{k}", errs)
    elif t == "string" and isinstance(v, str):
        if "pattern" in m:
            try:
                if not re.fullmatch(m["pattern"], v, re.S):
                    errs.append(f"{path}: {v!r} does not match {m['pattern']}")
            except re.error:
                pass   # a few service patterns use syntax Python's re lacks
        if len(v) < m.get("min", 0) or len(v) > m.get("max", 10**9):
            errs.append(f"{path}: length {len(v)}, allowed {m.get('min')}..{m.get('max')}")
        if shape.enum and v not in shape.enum:
            errs.append(f"{path}: {v!r} is not one of {shape.enum}")
    elif t in ("integer", "long", "double", "float") and isinstance(v, (int, float)) and not isinstance(v, bool):
        if v < m.get("min", v) or v > m.get("max", v):
            errs.append(f"{path}: {v}, allowed {m.get('min')}..{m.get('max')}")


def _validate(service: str, operation: str, params: dict) -> None:
    client = SESSION.client(service)
    op = client.meta.service_model.operation_model(operation)
    report = ParamValidator().validate(params, op.input_shape)
    assert not report.has_errors(), report.generate_report()
    errs: list[str] = []
    _deep(op.input_shape, params, operation, errs)
    assert not errs, "\n".join(errs)


def test_deep_check_catches_bad_map_key():
    body = qd.findings_dataset("123456789012", "arn:aws:quicksight:us-east-1:123456789012:datasource/x",
                               "floor_compliance", "arn:aws:quicksight:us-east-1:123456789012:user/default/me")
    body["LogicalTableMap"] = {"findings_l": next(iter(body["LogicalTableMap"].values()))}
    with pytest.raises(AssertionError, match="does not match"):
        _validate("quicksight", "CreateDataSet", body)


ACC, P = "123456789012", "arn:aws:quicksight:us-east-1:123456789012:user/default/me"
DS = "arn:aws:quicksight:us-east-1:123456789012:datasource/floor-compliance-athena"


def test_quicksight_data_source_shape():
    _validate("quicksight", "CreateDataSource", qd.data_source(ACC, "floor-compliance-athena", "floor-compliance", P))


def test_quicksight_dataset_shapes():
    _validate("quicksight", "CreateDataSet", qd.findings_dataset(ACC, DS, "floor_compliance", P))
    _validate("quicksight", "CreateDataSet", qd.deviations_dataset(ACC, DS, "floor_compliance", P))


def test_quicksight_dashboard_shape():
    body = qd.dashboard(ACC, "floor-compliance", DS.replace("datasource/floor-compliance-athena", "dataset/f"),
                        DS.replace("datasource/floor-compliance-athena", "dataset/d"), P)
    _validate("quicksight", "CreateDashboard", body)
    upd = {k: v for k, v in body.items() if k != "Permissions"}
    _validate("quicksight", "UpdateDashboard", upd)
    ids = {v[list(v)[0]]["VisualId"] for v in body["Definition"]["Sheets"][0]["Visuals"]}
    layout_ids = {e["ElementId"] for e in body["Definition"]["Sheets"][0]["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]}
    assert ids == layout_ids


def test_exported_json_matches_code():
    exported = json.loads((ROOT / "quick" / "dashboard-definition.json").read_text())
    assert exported["Definition"]["Sheets"][0]["Visuals"] == \
        qd.dashboard("<account-id>", "floor-compliance", "<findings-dataset-arn>", "<deviations-dataset-arn>",
                     "<quicksight-user-arn>")["Definition"]["Sheets"][0]["Visuals"]


def test_agent_registry_shapes():
    try:
        SESSION.client("agent-registry-control")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"boto3 here lacks agent-registry-control: {exc}")
    server = {"name": "com.example.plant/floor-compliance-agent", "description": "d", "version": "1.0.0",
              "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}]}
    _validate("agent-registry-control", "CreateRegistryRecord", dict(
        registryId="abcdefghijkl", name="floor-compliance-agent", displayName="Floor compliance agent",
        description="x", recordType="MCP", recordVersion="1.0",
        descriptors={"mcpServer": {"data": json.dumps(server), "dataSchemaVersion": "2025-12-11"}}))
    _validate("agent-registry-control", "CreateRegistry", dict(
        name="quick-agents", description="x", approvalConfiguration={"autoApprovalRules": ["APPROVE_ALL"]},
        discoveryConfiguration={"authorizerType": "AWS_IAM"}))
    _validate("agent-registry-control", "SubmitRegistryRecordForApproval", dict(registryId="abcdefghijkl", recordId="abcdefghijkl"))
    _validate("agent-registry-control", "UpdateRegistryRecordStatus",
              dict(registryId="abcdefghijkl", recordId="abcdefghijkl", status="APPROVED", statusReason="ok"))


def test_server_description_fits_registry_limit():
    sys.path.insert(0, str(ROOT / "scripts"))
    import floor
    assert len(floor.SERVER_DESCRIPTION) <= 100


def test_cfn_shapes():
    _validate("cloudformation", "CreateStack", dict(
        StackName="s", TemplateBody=(ROOT / "cfn" / "floor-compliance.yaml").read_text(),
        Parameters=[{"ParameterKey": "CodeS3Key", "ParameterValue": ""}], Capabilities=["CAPABILITY_NAMED_IAM"],
        OnFailure="DELETE"))


def test_dashboard_command_sends_valid_requests_on_create_and_update(monkeypatch, tmp_path):
    """Run the whole dashboard command twice against a Quick Sight client whose network layer is replaced.
    Every request is checked against the service model, so a field that only create accepts cannot reach update."""
    import floor
    from types import SimpleNamespace
    qs = SESSION.client("quicksight")
    exists: set[str] = set()
    calls: list[str] = []
    arn = "arn:aws:quicksight:us-east-1:123456789012:"

    def fake(operation, params):
        op = qs.meta.service_model.operation_model(operation)
        report = ParamValidator().validate(params, op.input_shape)
        assert not report.has_errors(), f"{operation}: {report.generate_report()}"
        errs: list[str] = []
        _deep(op.input_shape, params, operation, errs)
        assert not errs, "\n".join(errs)
        calls.append(operation)
        key = params.get("DataSourceId") or params.get("DataSetId") or params.get("DashboardId")
        kind = "datasource" if "DataSourceId" in params else "dataset" if "DataSetId" in params else "dashboard"
        if operation.startswith("Describe"):
            if key not in exists:
                raise qs.exceptions.ResourceNotFoundException({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, operation)
            if kind == "datasource":
                return {"DataSource": {"Arn": f"{arn}datasource/{key}", "Status": "CREATION_SUCCESSFUL"}}
            if kind == "dataset":
                return {"DataSet": {"Arn": f"{arn}dataset/{key}"}}
            return {"Dashboard": {"Version": {"Status": "CREATION_SUCCESSFUL", "VersionNumber": 2}}}
        if operation.startswith("Create"):
            exists.add(key)
            return {"Arn": f"{arn}{kind}/{key}"}
        if operation == "UpdateDashboard":
            return {"VersionArn": f"{arn}dashboard/{key}/version/2"}
        return {}

    monkeypatch.setattr(qs, "_make_api_call", fake)
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"})
    sess = SimpleNamespace(region_name="us-east-1", client=lambda name, **k: qs if name == "quicksight" else sts)
    monkeypatch.setattr(floor, "session_for", lambda region: sess)
    monkeypatch.setattr(floor, "STATE_FILE", tmp_path / "floor-env.json")
    monkeypatch.setattr(floor.time, "sleep", lambda s: None)
    floor.save_state({"region": "us-east-1", "outputs": {"AthenaWorkGroup": "floor-compliance", "BucketName": "b",
                                                          "GlueDatabase": "floor_compliance"}})
    args = SimpleNamespace(quicksight_user=P, project="floor-compliance")
    floor.cmd_dashboard(args)
    assert calls.count("CreateDataSet") == 2 and "CreateDashboard" in calls and "UpdateDataSource" not in calls
    calls.clear()
    floor.cmd_dashboard(args)      # second run takes the update path for all four resources
    assert {"UpdateDataSource", "UpdateDataSet", "UpdateDashboard", "UpdateDashboardPublishedVersion"} <= set(calls)
    assert not any(c.startswith("Create") for c in calls)


def test_registry_record_update_shape_and_quick_safe_descriptions():
    """A second deploy updates the record. The update call nests every field in optionalValue,
    and AWS Quick rejects connector descriptions with characters outside its allowed set."""
    import floor
    server = {"name": floor.SERVER_NAME, "description": floor.SERVER_DESCRIPTION, "version": "1.0.0",
              "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}]}
    _validate("agent-registry-control", "UpdateRegistryRecord",
              floor.record_update_body("abcdefghijkl", "abcdefghijkl", server))
    allowed = re.compile(r"[A-Za-z0-9 .,!?_-]+")
    assert allowed.fullmatch(floor.RECORD_DESCRIPTION) and allowed.fullmatch(floor.SERVER_DESCRIPTION)
