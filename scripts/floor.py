#!/usr/bin/env python3
"""Deploy, seed, check, dashboard and clean up the floor compliance solution.

Run through the wrappers (./deploy.sh, ./seed.sh, ./status.sh, ./dashboard.sh,
./cleanup.sh) from AWS CloudShell or any machine with AWS credentials.
State between commands lives in floor-env.json next to this repo's README.

    python scripts/floor.py deploy   [--region R] [--stack-name N] [--registry-id ID] [--skip-registry]
    python scripts/floor.py seed     [--sample | --manifest FILE | --havid-segments FILE --havid-context FILE] [--clips-dir DIR] [--local-analyze]
    python scripts/floor.py status
    python scripts/floor.py dashboard [--quicksight-user ARN]
    python scripts/floor.py cleanup  [--yes]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import boto3
import requests
from botocore.exceptions import ClientError, WaiterError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE_FILE = ROOT / "floor-env.json"
TEMPLATE = ROOT / "cfn" / "floor-compliance.yaml"
RECORD_NAME = "floor-compliance-agent"
SERVER_NAME = "com.example.plant/floor-compliance-agent"
# AWS Quick accepts only letters, numbers, spaces and . , ! ? - _ in a connector description.
SERVER_DESCRIPTION = "Floor compliance agent. Deviations, worker scorecards, clips, human-robot task planning"  # <= 100 chars
RECORD_DESCRIPTION = "Floor compliance agent for AWS Quick, NVIDIA Nemotron on AgentCore"
REGISTRY_NAME = "quick-agents"


# ----------------------------------------------------------------- helpers
def say(msg: str) -> None:
    print(msg, flush=True)


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def session_for(region: str | None) -> boto3.Session:
    s = boto3.Session(region_name=region)
    if not s.region_name:
        sys.exit("No AWS Region set. Pass --region or set AWS_REGION. Use the Region of your AWS Quick account.")
    return s


def outputs_of(cfn, stack: str) -> dict:
    desc = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])}


def stack_exists(cfn, stack: str) -> bool:
    try:
        st = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        return st != "REVIEW_IN_PROGRESS"
    except ClientError as e:
        if "does not exist" in str(e):
            return False
        raise


def deploy_stack(cfn, stack: str, params: dict) -> dict:
    body = TEMPLATE.read_text(encoding="utf-8")
    kw = dict(StackName=stack, TemplateBody=body,
              Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()],
              Capabilities=["CAPABILITY_NAMED_IAM"])
    if stack_exists(cfn, stack):
        try:
            cfn.update_stack(**kw)
            say(f"Updating stack {stack} ...")
            waiter = "stack_update_complete"
        except ClientError as e:
            if "No updates are to be performed" in str(e):
                say(f"Stack {stack} is already up to date.")
                return outputs_of(cfn, stack)
            raise
    else:
        cfn.create_stack(**kw, OnFailure="DELETE")
        say(f"Creating stack {stack} ...")
        waiter = "stack_create_complete"
    try:
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    except Exception:
        say("Stack operation failed. Most recent failed events:")
        for ev in cfn.describe_stack_events(StackName=stack)["StackEvents"][:40]:
            if "FAILED" in ev.get("ResourceStatus", ""):
                say(f"  {ev['LogicalResourceId']}: {ev.get('ResourceStatusReason', '')}")
        raise SystemExit(1)
    return outputs_of(cfn, stack)


SOURCE_DIRS = ("agent", "perception")
SOURCE_FILES = ("main.py",)
CODE_RUNTIME = "PYTHON_3_12"          # must match the template's CodeConfiguration.Runtime


def source_hash() -> str:
    """Hash of the agent source and its requirements; names the code zip in S3."""
    h = hashlib.sha256()
    paths = [ROOT / f for f in SOURCE_FILES]
    for folder in SOURCE_DIRS:
        paths += sorted(p for p in (ROOT / folder).rglob("*") if p.is_file()
                        and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))
    for p in paths:
        h.update(p.relative_to(ROOT).as_posix().encode()); h.update(p.read_bytes())
    h.update(CODE_RUNTIME.encode())
    return h.hexdigest()[:12]


def build_code_zip() -> bytes:
    """Package main.py, the agent, perception and pinned dependencies for linux/arm64.

    Dependencies are fetched as prebuilt wheels for the runtime's platform, so
    this works from CloudShell or a laptop of any architecture, with no Docker.
    """
    work = Path(tempfile.mkdtemp(prefix="floor-code-"))
    try:
        say("Packaging the agent (pure Python, dependencies for linux/arm64) ...")
        # Every argument is a literal and there is no shell. The interpreter is this script's own
        # (executable=, with its folder first on PATH so a virtual environment is still detected),
        # the working directory is the temporary build folder, and the only input is this repo's
        # pinned requirements file, copied in under a fixed name.
        own_python = dict(os.environ, PATH=os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]))
        shutil.copy2(ROOT / "agent" / "requirements.txt", work / "requirements.txt")
        res = subprocess.run(
            ["python", "-m", "pip", "install", "--quiet", "--target", ".",
             "--platform", "manylinux2014_aarch64", "--only-binary=:all:", "--implementation", "cp",
             "--python-version", "3.12", "-r", "requirements.txt"],
            executable=sys.executable, env=own_python, cwd=work, capture_output=True, text=True)
        (work / "requirements.txt").unlink(missing_ok=True)
        if res.returncode != 0:
            sys.exit("pip could not fetch ARM64 wheels for the agent dependencies:\n" + res.stderr[-2000:])
        for folder in SOURCE_DIRS:
            shutil.copytree(ROOT / folder, work / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in SOURCE_FILES:
            shutil.copy2(ROOT / f, work / f)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(work.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                    z.write(p, p.relative_to(work).as_posix())
        data = buf.getvalue()
        say(f"  package size: {len(data) / 1e6:.1f} MB zipped")
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def mcp_url(region: str, runtime_arn: str) -> str:
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT")


# ----------------------------------------------------------------- agent registry
def registry_client(sess):
    try:
        return sess.client("agent-registry-control")
    except Exception as exc:  # old boto3 without the service model
        sys.exit("This boto3 does not know agent-registry-control. Run ./deploy.sh (it creates a fresh "
                 f"virtual environment with a current boto3) or upgrade boto3. ({exc})")


def _id_from_arn(arn: str, kind: str) -> str:
    return arn.rsplit(f"{kind}/", 1)[-1]


def ensure_registry(arc, registry_id: str | None, state: dict) -> str:
    if registry_id:
        return registry_id
    regs = arc.list_registries().get("registries", [])
    by_name = [r for r in regs if r.get("name") == REGISTRY_NAME]
    if by_name:
        rid = by_name[0]["registryId"]
        say(f"Using existing Agent Registry '{REGISTRY_NAME}' ({rid}).")
        return rid
    if regs:
        say("This account already has an Agent Registry. AWS Quick links one registry per account,")
        say("so use the one Quick is linked to if there is one:")
        for i, r in enumerate(regs, start=1):
            say(f"  {i}. {r.get('name')} ({r.get('registryId')})")
        if sys.stdin.isatty():
            ans = input(f"Type a number to use that registry, or press Enter to create '{REGISTRY_NAME}': ").strip()
            if ans.isdigit() and 1 <= int(ans) <= len(regs):
                rid = regs[int(ans) - 1]["registryId"]
                say(f"Using registry {regs[int(ans) - 1].get('name')} ({rid}).")
                return rid
        else:
            say(f"Creating '{REGISTRY_NAME}'. To use an existing one instead, re-run with --registry-id <id>.")
    resp = arc.create_registry(name=REGISTRY_NAME, description="Agents and MCP servers approved for AWS Quick",
                               discoveryConfiguration={"authorizerType": "AWS_IAM"},
                               approvalConfiguration={"autoApprovalRules": ["APPROVE_ALL"]})
    rid = _id_from_arn(resp["registryArn"], "registry")
    state["created_registry_id"] = rid
    say(f"Created Agent Registry '{REGISTRY_NAME}' ({rid}); waiting for READY ...")
    for _ in range(60):
        st = arc.get_registry(registryId=rid).get("status")
        if st == "READY":
            return rid
        if st and "FAIL" in st:
            sys.exit(f"Registry creation failed: {st}")
        time.sleep(5)
    sys.exit("Registry did not become READY in time.")


def record_update_body(registry_id: str, rec_id: str, server: dict) -> dict:
    """UpdateRegistryRecord wraps every optional field in {"optionalValue": ...}, unlike the create call."""
    opt = lambda v: {"optionalValue": v}  # noqa: E731
    return {
        "registryId": registry_id, "recordId": rec_id, "recordVersion": "1.0",
        "description": opt(RECORD_DESCRIPTION),
        "descriptors": opt({"mcpServer": opt({"data": opt(json.dumps(server)),
                                              "dataSchemaVersion": opt("2025-12-11")})}),
    }


def ensure_record(arc, registry_id: str, url: str, state: dict) -> str:
    server = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": SERVER_NAME,
        "description": SERVER_DESCRIPTION,
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": url}],
    }
    descriptors = {"mcpServer": {"data": json.dumps(server), "dataSchemaVersion": "2025-12-11"}}
    existing = next((r for r in arc.list_registry_records(registryId=registry_id).get("registryRecords", [])
                     if r.get("name") == RECORD_NAME), None)
    if existing:
        rec_id = existing["recordId"]
        say(f"Updating Agent Registry record {RECORD_NAME} ({rec_id}) with the current endpoint ...")
        try:
            arc.update_registry_record(**record_update_body(registry_id, rec_id, server))
        except ClientError as e:
            say(f"  update skipped: {e.response['Error'].get('Message', e)}")
    else:
        resp = arc.create_registry_record(
            registryId=registry_id, name=RECORD_NAME, displayName="Floor compliance agent",
            description=RECORD_DESCRIPTION,
            recordType="MCP", recordVersion="1.0", descriptors=descriptors)
        rec_id = _id_from_arn(resp["recordArn"], "record")
        say(f"Created Agent Registry record {RECORD_NAME} ({rec_id}).")
    status = _wait_record(arc, registry_id, rec_id, {"DRAFT", "APPROVED", "PENDING_APPROVAL", "REJECTED"})
    if status in ("DRAFT", "REJECTED"):
        arc.submit_registry_record_for_approval(registryId=registry_id, recordId=rec_id)
        status = _wait_record(arc, registry_id, rec_id, {"APPROVED", "PENDING_APPROVAL"})
    if status == "PENDING_APPROVAL":
        arc.update_registry_record_status(registryId=registry_id, recordId=rec_id, status="APPROVED",
                                          statusReason="Approved by deploy script")
        status = _wait_record(arc, registry_id, rec_id, {"APPROVED"})
    say(f"  record status: {status}")
    state["record_id"] = rec_id
    return rec_id


def _wait_record(arc, registry_id: str, rec_id: str, targets: set[str]) -> str:
    for _ in range(60):
        st = arc.get_registry_record(registryId=registry_id, recordId=rec_id).get("status", "")
        if st in targets:
            return st
        if "FAIL" in st:
            sys.exit(f"Registry record failed: {st}")
        time.sleep(3)
    sys.exit("Registry record did not reach the expected status in time.")


# ----------------------------------------------------------------- calling the agent like Quick does
def get_token(sm, secret_arn: str) -> str:
    sec = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    basic = base64.b64encode(f"{sec['client_id']}:{sec['client_secret']}".encode()).decode()
    r = requests.post(sec["token_url"], data={"grant_type": "client_credentials", "scope": sec["scope"]},
                      headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


async def _mcp_call(url: str, token: str, tool: str, args: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {token}",
               "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"seed-{uuid.uuid4()}-{uuid.uuid4().hex[:8]}"}
    async with streamablehttp_client(url, headers=headers, timeout=120, terminate_on_close=False) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            if res.isError:
                raise RuntimeError(res.content[0].text if res.content else "tool error")
            return json.loads(res.content[0].text)


def call_agent(state: dict, sess, tool: str, args: dict | None = None) -> dict:
    token = get_token(sess.client("secretsmanager"), state["outputs"]["QuickClientSecretArn"])
    return asyncio.run(_mcp_call(state["mcp_url"], token, tool, args or {}))


# ----------------------------------------------------------------- commands
def cmd_deploy(a) -> None:
    sess = session_for(a.region)
    region = sess.region_name
    cfn, s3 = sess.client("cloudformation"), sess.client("s3")
    state = load_state()
    state.update({"region": region, "stack": a.stack_name})

    say(f"Region {region}, stack {a.stack_name}")
    params = {"ProjectName": a.project, "ModelId": a.model_id, "LlmEnabled": "true" if not a.no_llm else "false"}
    existing_key = _param(cfn, a.stack_name, "CodeS3Key") if stack_exists(cfn, a.stack_name) else ""
    state["project"] = a.project
    outputs = deploy_stack(cfn, a.stack_name, {**params, "CodeS3Key": existing_key})

    key = f"code/agent-{source_hash()}.zip"
    if existing_key == key and not a.rebuild:
        say(f"Agent code {key} is already deployed; skipping the package step.")
    else:
        s3.put_object(Bucket=outputs["BucketName"], Key=key, Body=build_code_zip())
        say(f"  uploaded s3://{outputs['BucketName']}/{key}")
        outputs = deploy_stack(cfn, a.stack_name, {**params, "CodeS3Key": key})

    state["outputs"] = outputs
    state["mcp_url"] = mcp_url(region, outputs["AgentRuntimeArn"])
    save_state(state)

    if not a.skip_registry:
        arc = registry_client(sess)
        rid = ensure_registry(arc, a.registry_id or state.get("registry_id"), state)
        state["registry_id"] = rid
        ensure_record(arc, rid, state["mcp_url"], state)
        save_state(state)

    if not a.no_llm:
        check_model(sess, a.model_id)

    say("\nAWS resources are deployed.")
    if not a.no_seed:
        say("\nLoading the bundled sample data ...")
        cmd_seed(argparse.Namespace())
    if not a.no_dashboard:
        say("\nBuilding the Quick Sight dashboard ...")
        try:
            cmd_dashboard(argparse.Namespace(quicksight_user=a.quicksight_user, project=a.project))
        except SystemExit as exc:
            say("Dashboard not built yet. On a first deployment this is expected: Quick Sight has no access to the\n"
                "new bucket until you grant it in step 5. After step 5, run ./dashboard.sh.")
            say(f"  (detail: {str(exc)[:300]})")
        except Exception as exc:
            say(f"Dashboard skipped ({type(exc).__name__}: {exc}).\n  Run ./dashboard.sh after fixing it. Everything else is deployed.")
    next_steps(state)


def next_steps(state: dict) -> None:
    out = state["outputs"]
    say("\n" + "=" * 68)
    say("DONE. What to do next, in AWS Quick (whitepaper section 6):")
    say(f"  Step 2  Link the registry: Manage account > Permissions > AWS Agent Registry,")
    say(f"          turn on the registry with ID {state.get('registry_id', '(see above)')}.")
    say("  Step 3  Customize > Connectors > Create > Cloud connector, search 'floor-compliance-agent'.")
    say("          Choose Service-to-service OAuth. Run ./connector.sh for the Client ID and secret.")
    say("  Step 4  Create a chat agent, add the connector, paste quick/agent-instructions.md.")
    say("  Step 5  Permissions > AWS resources: select Amazon Athena and Amazon S3,")
    say(f"          bucket {out['BucketName']}, tick 'Write permission for Athena Workgroup'.")
    say("=" * 68)


def cmd_connector(a) -> None:
    """Print the three values the AWS Quick connector asks for, ready to paste."""
    state = load_state()
    if not state.get("outputs"):
        sys.exit("No deployment found. Run ./deploy.sh first.")
    sess = session_for(state["region"])
    sec = json.loads(sess.client("secretsmanager").get_secret_value(
        SecretId=state["outputs"]["QuickClientSecretArn"])["SecretString"])
    say("Paste these into the connector's Authenticate step (choose Service-to-Service OAuth):\n")
    say(f"  Client ID:      {sec['client_id']}")
    say(f"  Client secret:  {sec['client_secret']}")
    say(f"  Token URL:      {sec['token_url']}")
    say("\nThe client secret is a password. Keep it out of screenshots and clear the screen when done (type: clear).")


def _param(cfn, stack: str, key: str) -> str:
    for p in cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Parameters", []):
        if p["ParameterKey"] == key:
            return p["ParameterValue"]
    return ""


def cmd_seed(a) -> None:
    from agent import pipeline
    from agent.store import Store
    from perception import labels_mode

    state = load_state()
    if not state.get("outputs"):
        sys.exit("No deployment found. Run ./deploy.sh first.")
    sess = session_for(state["region"])
    bucket = state["outputs"]["BucketName"]

    from agent import rules
    os.environ["AWS_REGION"] = state["region"]
    try:
        proc = pipeline.load_procedure(Store(bucket=bucket))
    except Exception:
        proc = rules.load_procedure()
    manifest, havid = getattr(a, "manifest", None), getattr(a, "havid_segments", None)
    if manifest:
        rows, problems = labels_mode.read_manifest(manifest, proc)
        src = manifest
    elif havid:
        from perception import havid_mode
        if not a.havid_context:
            sys.exit("--havid-segments needs --havid-context (see perception/havid_mode.py).")
        raw, p1 = havid_mode.files_to_manifest(havid, a.havid_context, a.fps, a.havid_vocab)
        rows, p2 = labels_mode.manifest_rows_to_actions(raw, proc)
        problems = p1 + p2
        src = havid
    else:
        rows, problems = labels_mode.read_manifest(ROOT / "data" / "sample_manifest.csv", proc)
        src = "data/sample_manifest.csv (bundled synthetic sample)"
    for p in problems[:20]:
        say(f"  manifest warning: {p}")
    if not rows:
        sys.exit("Manifest produced no action rows.")
    say(f"Loaded {len(rows)} action rows for {len({r['unit_id'] for r in rows})} units from {src}")

    if getattr(a, "clips_dir", None):
        s3 = sess.client("s3")
        n = 0
        for p in Path(a.clips_dir).rglob("*.mp4"):
            s3.upload_file(str(p), bucket, f"clips/{p.relative_to(a.clips_dir).as_posix()}")
            n += 1
        say(f"Uploaded {n} clips to s3://{bucket}/clips/")

    os.environ["AWS_REGION"] = state["region"]
    store = Store(bucket=bucket)
    added = pipeline.ingest_actions(store, rows)
    say(f"Wrote {added} new action rows to s3://{bucket}/actions/actions.csv")

    res = pipeline.analyze(store, only_new=True)
    say(f"Analyzed: {json.dumps(res)}")
    if getattr(a, "local_analyze", False):
        return
    check_agent(state, sess)


def check_agent(state: dict, sess, attempts: int = 4, wait: int = 20) -> bool:
    """Call the agent the way AWS Quick will. A new runtime can take a minute to answer."""
    say("Checking the agent through the endpoint AWS Quick will use ...")
    last: Exception | None = None
    for i in range(attempts):
        try:
            f = call_agent(state, sess, "list_filters")
            say(f"  agent answered: {f.get('units_total', '?')} units, {len(f.get('stations', []))} stations, "
                f"latest date {f.get('latest_date', '?')}")
            return True
        except Exception as exc:  # noqa: BLE001 - report and retry
            last = exc
            if i < attempts - 1:
                time.sleep(wait)
    say(f"  WARNING: the agent did not answer ({type(last).__name__}: {last}).")
    say("  The data is loaded. Run ./status.sh in a minute; if it still fails, see README Troubleshooting.")
    return False


def check_model(sess, model_id: str) -> bool:
    """One small call with the deployer's credentials. For models served through AWS Marketplace the
    first call in an account also enables the model, which the agent's own role is not allowed to do."""
    try:
        sess.client("bedrock-runtime").converse(
            modelId=model_id, messages=[{"role": "user", "content": [{"text": "Reply with OK."}]}],
            inferenceConfig={"maxTokens": 8})
        say(f"Model check: {model_id} answered in {sess.region_name}.")
        return True
    except Exception as exc:  # noqa: BLE001
        say(f"Model check: {model_id} is not usable in {sess.region_name} ({type(exc).__name__}: {exc}).")
        say("  The agent still works; its narratives use rules text until the model is available. "
            "Pick a Region that offers the model, or pass --model-id.")
        return False


def cmd_status(a) -> None:
    state = load_state()
    if not state.get("outputs"):
        sys.exit("No deployment found. Run ./deploy.sh first.")
    sess = session_for(state["region"])
    say(f"Calling the agent at {state['mcp_url']} ...")
    filters = call_agent(state, sess, "list_filters")
    say(f"Units analyzed: {filters['units_total']}  dates: {filters['dates'][:1]} .. {filters['dates'][-1:]}")
    say(f"Stations: {', '.join(filters['stations'])}   Workers: {len(filters['workers'])}")
    summary = call_agent(state, sess, "get_shift_summary", {"date": a.date or "", "shift": a.shift or ""})
    say(f"\n{summary['date']} shift {summary['shift']}: {summary['units']} units, compliance {summary['compliance_pct']}%")
    say(f"  {'station':8} {'units':>5} {'compl%':>7} {'high':>5} {'med':>5} {'low':>5}")
    for s in summary["by_station"]:
        say(f"  {s['station']:8} {s['units']:>5} {str(s['compliance_pct']):>7} {s['high']:>5} {s['medium']:>5} {s['low']:>5}")
    say(f"  by type: {summary['deviations_by_type']}")
    say(f"\n  {summary['narrative']}  [{summary['narrative_source']}]")


def cmd_dashboard(a) -> None:
    import quicksight_defs as qd

    state = load_state()
    if not state.get("outputs"):
        sys.exit("No deployment found. Run ./deploy.sh first.")
    sess = session_for(state["region"])
    region = sess.region_name
    account = sess.client("sts").get_caller_identity()["Account"]
    qs = sess.client("quicksight")
    principal = a.quicksight_user or _pick_quicksight_user(sess, account)
    out = state["outputs"]

    ds_id = f"{a.project}-athena"
    ds_arn = _ensure(qs, "data_source", qd.data_source(account, ds_id, out["AthenaWorkGroup"], principal),
                     dict(AwsAccountId=account, DataSourceId=ds_id), "DataSourceArn", "DataSource")
    _wait_data_source(qs, account, ds_id, out["BucketName"])
    f_arn = _ensure(qs, "data_set", qd.findings_dataset(account, ds_arn, out["GlueDatabase"], principal),
                    dict(AwsAccountId=account, DataSetId="floor-compliance-findings"), "Arn", "DataSet")
    d_arn = _ensure(qs, "data_set", qd.deviations_dataset(account, ds_arn, out["GlueDatabase"], principal),
                    dict(AwsAccountId=account, DataSetId="floor-compliance-deviations"), "Arn", "DataSet")
    dash_id = a.project
    body = qd.dashboard(account, dash_id, f_arn, d_arn, principal)
    try:
        qs.describe_dashboard(AwsAccountId=account, DashboardId=dash_id)
        resp = qs.update_dashboard(**_update_body(qs, "update_dashboard", body))
        ver = int(resp["VersionArn"].rsplit("/", 1)[1])
        _wait_dashboard(qs, account, dash_id, ver)
        qs.update_dashboard_published_version(AwsAccountId=account, DashboardId=dash_id, VersionNumber=ver)
        say("Dashboard updated.")
    except qs.exceptions.ResourceNotFoundException:
        qs.create_dashboard(**body)
        _wait_dashboard(qs, account, dash_id, None)
        say("Dashboard created.")
    state["dashboard_id"] = dash_id
    save_state(state)
    say(f"Open it: https://{region}.quicksight.aws.amazon.com/sn/dashboards/{dash_id}")
    say("If a visual shows a permission error, re-check step 5 (Athena and the findings bucket under Permissions, AWS resources).")


def _update_body(qs, method: str, body: dict) -> dict:
    """Update calls accept fewer fields than create calls (no Type, no Permissions). Keep only what the API takes."""
    op = qs.meta.service_model.operation_model(qs.meta.method_to_api_mapping[method])
    return {k: v for k, v in body.items() if k in op.input_shape.members}


def _wait_dashboard(qs, account: str, dash_id: str, version: int | None) -> None:
    kw = {"VersionNumber": version} if version else {}
    for _ in range(40):
        v = qs.describe_dashboard(AwsAccountId=account, DashboardId=dash_id, **kw)["Dashboard"]["Version"]
        st = v["Status"]
        if st.endswith("SUCCESSFUL"):
            return
        if st.endswith("FAILED"):
            errs = "; ".join(f"{e.get('Type')}: {e.get('Message')}" for e in v.get("Errors", [])) or st
            if version is None:      # a failed first version blocks later updates; remove it so a re-run starts clean
                try:
                    qs.delete_dashboard(AwsAccountId=account, DashboardId=dash_id)
                except ClientError:
                    pass
            sys.exit(f"Quick Sight rejected the dashboard definition: {errs}")
        time.sleep(3)
    sys.exit("The dashboard did not finish building in time. Run ./dashboard.sh again in a minute.")


def _ensure(qs, kind: str, body: dict, ident: dict, arn_key: str, label: str) -> str:
    describe = getattr(qs, f"describe_{kind}")
    create = getattr(qs, f"create_{kind}")
    update = getattr(qs, f"update_{kind}")
    try:
        resp = describe(**ident)
        arn = resp[label]["Arn"]
        update(**_update_body(qs, f"update_{kind}", body))
        say(f"{label} {ident[list(ident)[1]]} updated.")
        return arn
    except qs.exceptions.ResourceNotFoundException:
        resp = create(**body)
        say(f"{label} {ident[list(ident)[1]]} created.")
        return resp["Arn"]


def _wait_data_source(qs, account: str, ds_id: str, bucket: str) -> None:
    for _ in range(30):
        ds = qs.describe_data_source(AwsAccountId=account, DataSourceId=ds_id)["DataSource"]
        st = ds.get("Status", "")
        if st.endswith("SUCCESSFUL"):
            return
        if st.endswith("FAILED"):
            try:
                qs.delete_data_source(AwsAccountId=account, DataSourceId=ds_id)
            except ClientError:
                pass
            sys.exit("Quick Sight could not connect to Athena "
                     f"({ds.get('ErrorInfo', {}).get('Message', st)}). In the Quick administration console, under "
                     "Permissions, AWS resources, select Amazon Athena and Amazon S3, choose the bucket "
                     f"{bucket} and tick 'Write permission for Athena Workgroup'. Then run ./dashboard.sh.")
        time.sleep(2)


def _pick_quicksight_user(sess, account: str) -> str:
    """Prefer the Quick user that matches the caller, then an administrator, then the first user."""
    caller = sess.client("sts").get_caller_identity()["Arn"]
    tail = caller.split(":", 5)[-1]                      # user/name or assumed-role/Role/session
    wanted = tail.split("/", 1)[1] if "/" in tail else tail
    for region in dict.fromkeys(("us-east-1", sess.region_name)):
        try:
            users = sess.client("quicksight", region_name=region).list_users(AwsAccountId=account, Namespace="default")["UserList"]
        except ClientError:
            continue
        if not users:
            continue
        match = next((u for u in users if u["UserName"] == wanted), None)
        admin = next((u for u in users if u.get("Role", "").startswith("ADMIN")), None)
        pick = match or admin or users[0]
        if not match and len(users) > 1:
            say(f"No Quick user matches your sign-in ({wanted}); using {pick['UserName']}. "
                "Pass --quicksight-user <arn> to choose another.")
        return pick["Arn"]
    sys.exit("Could not list Quick users. Pass --quicksight-user arn:aws:quicksight:<region>:<account>:user/default/<name>")


def cmd_cleanup(a) -> None:
    state = load_state()
    if not state:
        sys.exit("Nothing to clean up (no floor-env.json).")
    sess = session_for(state.get("region"))
    if not a.yes:
        ans = input(f"Delete stack {state['stack']}, its bucket contents, the registry record and the dashboard? [y/N] ")
        if ans.strip().lower() != "y":
            return
    account = sess.client("sts").get_caller_identity()["Account"]
    if state.get("dashboard_id"):
        qs = sess.client("quicksight")
        for fn, ident in (("delete_dashboard", {"DashboardId": state["dashboard_id"]}),
                          ("delete_data_set", {"DataSetId": "floor-compliance-findings"}),
                          ("delete_data_set", {"DataSetId": "floor-compliance-deviations"}),
                          ("delete_data_source", {"DataSourceId": f"{state.get('project', 'floor-compliance')}-athena"})):
            try:
                getattr(qs, fn)(AwsAccountId=account, **ident)
            except ClientError:
                pass
        say("Quick Sight dashboard and datasets removed.")
    created = state.get("created_registry_id") or (state.get("registry_id") if state.get("created_registry") else None)
    if state.get("registry_id") or created:
        arc = sess.client("agent-registry-control")
        try:
            if state.get("registry_id") and state.get("record_id"):
                arc.delete_registry_record(registryId=state["registry_id"], recordId=state["record_id"])
                say("Agent Registry record deleted.")
            if created:      # only a registry this tool created is ever deleted
                for r in arc.list_registry_records(registryId=created).get("registryRecords", []):
                    if r.get("name") == RECORD_NAME:
                        try:
                            arc.delete_registry_record(registryId=created, recordId=r["recordId"])
                        except ClientError:
                            pass
                time.sleep(5)
                arc.delete_registry(registryId=created)
                say(f"Agent Registry {created} deleted.")
        except ClientError as e:
            say(f"Registry cleanup: {e}")
    outputs = state.get("outputs", {})
    bucket = outputs.get("BucketName")
    logs_bucket = outputs.get("AccessLogsBucketName")
    s3 = sess.resource("s3")
    if bucket and logs_bucket:
        try:  # stop new access logs before the log bucket is emptied
            sess.client("s3").put_bucket_logging(Bucket=bucket, BucketLoggingStatus={})
        except ClientError as e:
            say(f"Could not turn off access logging: {e}")

    def empty(name: str) -> None:
        """Buckets are versioned, so every version and delete marker has to go."""
        try:
            s3.Bucket(name).object_versions.all().delete()
            say(f"Emptied s3://{name}")
        except ClientError as e:
            say(f"Bucket empty skipped for {name}: {e}")

    for name in (bucket, logs_bucket):
        if name:
            empty(name)
    cfn = sess.client("cloudformation")
    waiter = cfn.get_waiter("stack_delete_complete")
    for attempt in (1, 2):
        cfn.delete_stack(StackName=state["stack"])
        say(f"Deleting stack {state['stack']} ...")
        try:
            waiter.wait(StackName=state["stack"], WaiterConfig={"Delay": 15, "MaxAttempts": 80})
            break
        except WaiterError:
            if attempt == 2:
                raise
            # S3 can deliver access logs a little late, which leaves the log bucket non-empty.
            say("Stack delete did not finish. Emptying the buckets again and retrying once ...")
            for name in (bucket, logs_bucket):
                if name:
                    empty(name)
    STATE_FILE.unlink(missing_ok=True)
    say("Done. In AWS Quick, delete the connector and chat agent, and turn off the registry link if no longer needed.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy")
    d.add_argument("--region")
    d.add_argument("--stack-name", default="floor-compliance")
    d.add_argument("--project", default="floor-compliance")
    d.add_argument("--model-id", default="nvidia.nemotron-nano-3-30b")
    d.add_argument("--no-llm", action="store_true", help="run the agent without Bedrock narratives")
    d.add_argument("--registry-id", help="reuse an existing AWS Agent Registry (IAM auth)")
    d.add_argument("--skip-registry", action="store_true")
    d.add_argument("--no-seed", action="store_true", help="do not load the bundled sample data")
    d.add_argument("--no-dashboard", action="store_true", help="do not build the Quick Sight dashboard")
    d.add_argument("--quicksight-user", help="Quick Sight user ARN that will own the dashboard")
    d.add_argument("--rebuild", action="store_true", help="repackage and upload the code even if unchanged")
    d.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("seed")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--sample", action="store_true", help="bundled sample manifest (default)")
    g.add_argument("--manifest", help="your own action manifest CSV")
    g.add_argument("--havid-segments", help="HA-ViD action segments CSV (see perception/havid_mode.py)")
    s.add_argument("--havid-context", help="plant context CSV: video_id, worker, station, date, shift, start_time")
    s.add_argument("--havid-vocab", help="optional JSON mapping dataset labels to procedure names")
    s.add_argument("--fps", type=float, default=30.0, help="frame rate of the annotated videos")
    s.add_argument("--clips-dir", help="upload .mp4 clips under this folder to clips/")
    s.add_argument("--local-analyze", action="store_true", help="run the rules here instead of through the agent")
    s.set_defaults(fn=cmd_seed)

    st = sub.add_parser("status")
    st.add_argument("--date", default=""); st.add_argument("--shift", default="")
    st.set_defaults(fn=cmd_status)

    db = sub.add_parser("dashboard")
    db.add_argument("--quicksight-user", help="QuickSight user ARN that will own the dashboard")
    db.add_argument("--project", default="floor-compliance")
    db.set_defaults(fn=cmd_dashboard)

    cn = sub.add_parser("connector", help="print the values to paste into the AWS Quick connector")
    cn.set_defaults(fn=cmd_connector)

    c = sub.add_parser("cleanup")
    c.add_argument("--yes", action="store_true")
    c.set_defaults(fn=cmd_cleanup)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
