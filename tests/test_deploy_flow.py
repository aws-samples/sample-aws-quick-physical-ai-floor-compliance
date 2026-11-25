import argparse
"""Exercise the deploy orchestration against fake AWS clients (no account needed):
phase 1 stack, code package upload, phase 2 stack with the runtime, registry record, state file."""
import json
import sys
import zipfile
import io
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import floor  # noqa: E402


class FakeWaiter:
    def wait(self, **kw):
        pass


class FakeCfn:
    def __init__(self):
        self.stacks = {}
        self.calls = []

    def describe_stacks(self, StackName):
        if StackName not in self.stacks:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ValidationError", "Message": f"Stack with id {StackName} does not exist"}}, "DescribeStacks")
        p = self.stacks[StackName]
        outs = {"BucketName": "b", "UserPoolId": "us-east-1_abc", "QuickClientId": "cid",
                "QuickClientSecretArn": "arn:sec", "TokenUrl": "https://t/oauth2/token", "DiscoveryUrl": "https://d",
                "GlueDatabase": "floor_compliance", "AthenaWorkGroup": "floor-compliance"}
        if p.get("CodeS3Key"):
            outs["AgentRuntimeArn"] = "arn:aws:bedrock-agentcore:us-east-1:123:runtime/floor_compliance_agent-XYZ"
            outs["AgentRuntimeId"] = "floor_compliance_agent-XYZ"
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE",
                            "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in p.items()],
                            "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outs.items()]}]}

    def _params(self, kw):
        return {p["ParameterKey"]: p["ParameterValue"] for p in kw["Parameters"]}

    def create_stack(self, **kw):
        assert "CAPABILITY_NAMED_IAM" in kw["Capabilities"]
        self.stacks[kw["StackName"]] = self._params(kw)
        self.calls.append("create")

    def update_stack(self, **kw):
        if self.stacks[kw["StackName"]] == self._params(kw):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "No updates are to be performed."}}, "UpdateStack")
        self.stacks[kw["StackName"]] = self._params(kw)
        self.calls.append("update")

    def get_waiter(self, name):
        return FakeWaiter()


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body


class FakeRegistry:
    def __init__(self):
        self.records = {}
        self.status_calls = []

    def list_registries(self):
        return {"registries": []}

    def create_registry(self, **kw):
        assert kw["discoveryConfiguration"] == {"authorizerType": "AWS_IAM"}
        return {"registryArn": "arn:aws:agent-registry:us-east-1:123:registry/ABCDEFGHIJKL"}

    def get_registry(self, registryId):
        return {"status": "READY"}

    def list_registry_records(self, registryId):
        return {"registryRecords": [dict(recordId=k, name=v["name"]) for k, v in self.records.items()]}

    def create_registry_record(self, **kw):
        data = json.loads(kw["descriptors"]["mcpServer"]["data"])
        assert data["remotes"][0]["url"].startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/")
        assert "%3A" in data["remotes"][0]["url"]  # ARN is URL-encoded
        assert len(data["description"]) <= 100
        self.records["r1"] = {"name": kw["name"], "status": "DRAFT"}
        return {"recordArn": "arn:aws:agent-registry:us-east-1:123:registry/ABCDEFGHIJKL/record/r1", "status": "CREATING"}

    def update_registry_record(self, **kw):
        self.records[kw["recordId"]]["updated"] = True

    def get_registry_record(self, registryId, recordId):
        return {"status": self.records[recordId]["status"]}

    def submit_registry_record_for_approval(self, registryId, recordId):
        self.records[recordId]["status"] = "PENDING_APPROVAL"

    def update_registry_record_status(self, registryId, recordId, status, statusReason):
        self.records[recordId]["status"] = status
        self.status_calls.append(status)


def test_deploy_flow(monkeypatch, tmp_path):
    cfn, s3, reg = FakeCfn(), FakeS3(), FakeRegistry()
    packaged = []

    def fake_zip():
        packaged.append(1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("main.py", "x"); z.writestr("agent/server.py", "x"); z.writestr("mcp/__init__.py", "x")
        return buf.getvalue()
    monkeypatch.setattr(floor, "build_code_zip", fake_zip)

    class FakeSession:
        region_name = "us-east-1"

        def client(self, name, **kw):
            return {"cloudformation": cfn, "s3": s3, "agent-registry-control": reg}[name]

    monkeypatch.setattr(floor, "session_for", lambda region: FakeSession())
    monkeypatch.setattr(floor, "STATE_FILE", tmp_path / "floor-env.json")
    monkeypatch.setattr(floor.time, "sleep", lambda s: None)

    args = SimpleNamespace(no_seed=True, no_dashboard=True, quicksight_user=None, region="us-east-1", stack_name="floor-compliance", project="floor-compliance",
                           model_id="nvidia.nemotron-nano-3-30b", no_llm=False, registry_id=None,
                           skip_registry=False, rebuild=False)
    floor.cmd_deploy(args)

    assert cfn.calls == ["create", "update"]           # phase 1, then phase 2 with the code key
    key = cfn.stacks["floor-compliance"]["CodeS3Key"]
    assert key.startswith("code/agent-") and key.endswith(".zip") and key in s3.objects
    assert packaged == [1]
    assert reg.records["r1"]["status"] == "APPROVED" and reg.status_calls == ["APPROVED"]

    state = json.loads((tmp_path / "floor-env.json").read_text())
    assert state["registry_id"] == "ABCDEFGHIJKL" and state["record_id"] == "r1" and state["created_registry_id"] == "ABCDEFGHIJKL"
    assert state["mcp_url"].endswith("/invocations?qualifier=DEFAULT")

    # Second run: same source, no repackage, no stack change.
    cfn.calls.clear()
    floor.cmd_deploy(args)
    assert cfn.calls == [] and packaged == [1]
    assert reg.records["r1"]["updated"] and len(reg.records) == 1


def test_mcp_url_encodes_arn():
    url = floor.mcp_url("us-west-2", "arn:aws:bedrock-agentcore:us-west-2:123:runtime/x-1")
    assert url == ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
                   "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123%3Aruntime%2Fx-1/invocations?qualifier=DEFAULT")


def test_deploy_seeds_and_survives_dashboard_failure(monkeypatch, tmp_path, capsys):
    """Seeding analyzes the sample and then checks the agent through the path AWS Quick uses."""
    import agent.store as store_mod
    real_store = store_mod.Store
    monkeypatch.setattr(store_mod, "Store", lambda bucket=None, **kw: real_store(data_dir=str(tmp_path / "data")))
    monkeypatch.setattr(floor, "STATE_FILE", tmp_path / "floor-env.json", raising=False)
    floor.save_state({"region": "us-east-1", "outputs": {"BucketName": "b"}})
    calls = []
    monkeypatch.setattr(floor, "session_for", lambda region: SimpleNamespace(region_name=region, client=lambda n: None))
    monkeypatch.setattr(floor, "call_agent", lambda state, sess, tool, args=None: calls.append(tool) or {"units_total": 1792, "stations": ["ST-01"]})
    floor.cmd_seed(argparse.Namespace())
    assert calls == ["list_filters"]
    st = real_store(data_dir=str(tmp_path / "data"))
    assert len(st.read_csv("findings/findings.csv")) == 1792
    rows = st.read_csv("actions/actions.csv")
    assert len({r["unit_id"] for r in rows}) == 1792 and rows[0]["object"] == "base_plate"


def test_agent_check_reports_failure_without_stopping(monkeypatch):
    monkeypatch.setattr(floor.time, "sleep", lambda s: None)
    def boom(*a, **k):
        raise RuntimeError("not ready")
    monkeypatch.setattr(floor, "call_agent", boom)
    assert floor.check_agent({}, None, attempts=2) is False


def test_quick_user_pick_prefers_caller(monkeypatch):
    users = [{"UserName": "someone-else", "Arn": "arn:1", "Role": "READER"},
             {"UserName": "Admin/ebbey", "Arn": "arn:2", "Role": "ADMIN_PRO"},
             {"UserName": "boss", "Arn": "arn:3", "Role": "ADMIN"}]
    class C:
        def __init__(self, arn): self.arn = arn
        def get_caller_identity(self): return {"Arn": self.arn}
        def list_users(self, **k): return {"UserList": users}
    def sess(arn):
        c = C(arn)
        return SimpleNamespace(region_name="us-east-1", client=lambda name, **k: c)
    assert floor._pick_quicksight_user(sess("arn:aws:sts::1:assumed-role/Admin/ebbey"), "1") == "arn:2"
    assert floor._pick_quicksight_user(sess("arn:aws:iam::1:user/nobody"), "1") == "arn:2"   # first administrator


def test_connector_prints_three_values(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(floor, "STATE_FILE", tmp_path / "floor-env.json", raising=False)
    floor.save_state({"region": "us-east-1", "outputs": {"QuickClientSecretArn": "arn:secret", "BucketName": "b"}})
    sm = SimpleNamespace(get_secret_value=lambda SecretId: {"SecretString": json.dumps(
        {"client_id": "abc123", "client_secret": "s3cr3t", "token_url": "https://x.auth/oauth2/token", "scope": "floor-agent/invoke"})})
    monkeypatch.setattr(floor, "session_for", lambda region: SimpleNamespace(region_name=region, client=lambda n: sm))
    floor.cmd_connector(argparse.Namespace())
    out = capsys.readouterr().out
    assert "abc123" in out and "s3cr3t" in out and "https://x.auth/oauth2/token" in out
    floor.next_steps({"outputs": {"BucketName": "bkt-1"}, "registry_id": "ABCDEFGHIJKL"})
    out = capsys.readouterr().out
    assert "bkt-1" in out and "./connector.sh" in out and "s3cr3t" not in out
