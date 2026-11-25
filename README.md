# Physical AI floor compliance with AWS Quick, NVIDIA Nemotron 3 Nano and Infosys

Reference implementation for the whitepaper *Monitor worker safety and compliance on the plant floor with AWS Quick*. Camera footage of an assembly station becomes a findings table; fixed rules score it and NVIDIA Nemotron 3 Nano writes the summary; AWS Quick is where the operations manager asks questions, reads the dashboard, downloads a report and emails a worker.

```
cameras ─► Amazon S3 ─► perception (action labels or a vision model) ─► action sequence
        ─► rules engine + Nemotron agent on AgentCore Runtime ─► findings tables in S3
        ─► AWS Agent Registry ─► AWS Quick (chat agent, Quick Sight dashboard, PDF, email, embed)
```

One command (`./deploy.sh`) builds the AWS side in about 15 minutes and loads sample data. Three short steps in the AWS Quick console finish it. No Docker, no container registry, no build server, no GPU: the agent ships as a zip of Python code that Amazon Bedrock AgentCore Runtime runs directly, and every component is serverless, so the stack costs little when nobody is using it.

## What is real and what is a stand-in

| Part | In this deployment | In production |
| --- | --- | --- |
| Agent, rules, storage, registry, Quick chat agent, dashboard, PDF, email | Deployed and working | Same |
| Camera video and vision model | Stand-in: action labels arrive as a CSV, in the five-element form HA-ViD uses (subject, verb, object, target, tool) | A vision and action model on the plant's cameras writes the same rows |
| Plant data | Stand-in: a synthetic sample (4 stations, 8 workers, 14 days). Station, shift, date and posture values are generated | The plant's own recordings |
| Robot timings for task planning | Stand-in: a placeholder table, flagged as such in every answer | Measured in an NVIDIA Isaac Sim digital twin on a GPU instance |

## What you get

| Layer | What runs | Where in this repo |
| --- | --- | --- |
| Perception | Action labels as the perception output (`labels` mode), a converter for HA-ViD segments (`havid` mode), and an optional hook for a hosted NVIDIA vision model (`nim` mode, not exercised here). | `perception/` |
| Reasoning | Deterministic rules against a plant-owned procedure file; NVIDIA Nemotron 3 Nano on Amazon Bedrock writes the narrative and never sets a number. | `agent/rules.py`, `agent/llm.py`, `agent/procedure/` |
| Agent | MCP server on Amazon Bedrock AgentCore Runtime (direct code deployment, Python 3.12), registered in AWS Agent Registry. Tools: `get_shift_summary`, `list_deviations`, `get_worker_scorecard`, `get_station_trend`, `get_clip_evidence`, `get_procedure`, `list_filters`, `analyze_pending`, and for planning `get_step_profile`, `compare_task_allocation`. | `agent/server.py` |
| Operations | AWS Quick chat agent instructions, Quick Sight dashboard, Outlook or Gmail action connector, embed page. | `quick/`, `site/` |
| Decision (section 5) | Human-robot allocation calculator, served to Quick by the same agent, and the digital twin guide. | `agent/planning/`, `digital-twin/` |

## Before you start

- An AWS account with an AWS Quick subscription that includes custom MCP connectors, and a user who is a Quick administrator. That administrator's IAM identity needs `agent-registry:ListRegistries`, or the registry will not be listed in step 2.
- One AWS Region for everything: Quick, this stack and the Agent Registry. The registry link does not work across Regions.
- Permission to create CloudFormation stacks (S3, Cognito, IAM roles, Secrets Manager, Glue, Athena, AgentCore Runtime, Agent Registry).
- A Region where Amazon Bedrock offers `nvidia.nemotron-nano-3-30b`. Bedrock no longer has a model access page: serverless models are enabled by default, and `deploy.sh` makes one test call with your credentials to confirm. If the model is not available the agent still works; narratives fall back to rules text.
- For the email step, a Microsoft 365 or Google Workspace account.

## Deploy

All commands run in [AWS CloudShell](https://console.aws.amazon.com/cloudshell) (terminal icon in the console top bar). Nothing is installed on your computer. The scripts create a private Python environment in `.venv` on first run.

**Step 1: Deploy (about 15 minutes, one command)**

```bash
git clone https://github.com/aws-samples/sample-aws-quick-physical-ai-floor-compliance.git
cd sample-aws-quick-physical-ai-floor-compliance
./deploy.sh
```

`deploy.sh` does five things in order and prints each one:

1. Creates the CloudFormation stack (bucket, sign-in for Quick, tables).
2. Packages the agent and starts it on AgentCore Runtime.
3. Registers the agent in AWS Agent Registry (creating a registry named `quick-agents` if you have none; if the account already has one, the script lists it and asks which to use, since Quick links one registry per account).
4. Loads and analyzes the bundled sample (4 stations, 8 workers, 14 days, 1,792 units), then calls the agent through the endpoint Quick will use.
5. Builds the "Floor compliance" Quick Sight dashboard for the Quick user that matches your sign-in (pass `--quicksight-user <arn>` to choose the owner).

It ends with a "What to do next" summary that names the registry and the bucket you will pick in the Quick console. On a first deployment the script reports "Dashboard not built yet". That is expected: Quick Sight cannot read the new bucket until you grant access in step 5, and `./dashboard.sh` builds it then.

The scripts need Python 3.10 or newer. In CloudShell they install Python 3.12 on first run if the default is older.

Check it: `./status.sh` asks the agent for the latest shift summary through the same endpoint Quick uses.

**Step 2: Link the Agent Registry to AWS Quick (5 minutes, Quick administrator)**

In the Quick administration console: Manage account, then Permissions, then AWS Agent Registry. Turn on the toggle for the registry named in the deploy summary (`quick-agents` unless you chose an existing one) and confirm. If Quick was linked to a different registry, connectors already created from it keep working, but its other cards leave the Create for your team tab.

**Step 3: Create the connector (5 minutes)**

1. In Quick open Customize, then the Connectors tab. Choose Create, then Cloud connector, and search for `floor-compliance-agent`. The card comes from the registry with its name, description and endpoint filled in. Choose Continue.
2. For Auth configuration choose Service-to-service OAuth. The Token URL is already filled in. In CloudShell run `./connector.sh` and paste the Client ID and Client secret it prints. The client secret is a password: keep it out of screenshots and type `clear` afterwards.
3. Leave all tools enabled, Create and continue, then share the connector with the user group that will use it.

**Step 4: Create the chat agent (10 minutes)**

1. In Quick open Customize, then the Agents tab. Choose Create, then From chat. Quick opens an agent builder conversation.
2. Tell it what you want in one message: `Create from scratch. Name: Floor Compliance Assistant. Use only the floor-compliance-agent connector. No schedule. Use these custom instructions exactly as written:` followed by the block from `quick/agent-instructions.md` (in CloudShell, `cat quick/agent-instructions.md` and copy it).
3. The builder shows a draft and asks how to create it. Choose the option with web search off, so answers come only from the floor compliance tools.
4. Back on the Agents tab, open the menu on the draft's row and choose Chat. Test with "which stations had deviations in the last shift". You should get a sentence, a bar chart and a table. Then try "what if a robot did the fastening steps".
5. The agent is a draft that only you can see. Choose Publish when you want others to use it.

**Step 5: Give Quick Sight access to the data, then open the dashboard (5 minutes)**

1. One time, as a Quick administrator: in the Quick administration console, under Permissions, choose AWS resources. Select Amazon Athena and Amazon S3, choose the findings bucket from the deploy output, and tick "Write permission for Athena Workgroup". Save.
2. In CloudShell run `./dashboard.sh`. It builds the dashboard and prints its link.
3. Open "Floor compliance" in Quick Sight (compliance KPI, deviations by station and severity, by type and shift, per day, and the deviation log). Choose Export, then Generate PDF.

The definitions are in `quick/dataset-definition.json` and `quick/dashboard-definition.json` if you prefer to build by hand.

**Step 6 (optional): Email and embed**

- Email: Connectors, Available tab, Microsoft Outlook or Gmail, Connect, sign in. With the default OAuth app no identity-provider setup is needed; a Microsoft 365 administrator may need to grant one-time consent.
- Embed: follow `quick/embed-README.md` to place the chat agent and dashboard on `site/index.html`.

**Your own data**

`./seed.sh --manifest my_actions.csv` loads one row per action with the columns of `data/sample_manifest.csv`. For HA-ViD, see Data below.

**Clean up**

```bash
./cleanup.sh
```

Removes the dashboard and datasets, the registry record (and the registry if the script created it), empties the bucket and deletes the stack. Remove the connector and chat agent in Quick yourself, and turn off the registry link if nothing else uses it.

## How it works

**Perception.** The reference deployment takes action labels as the perception output: one row per action with unit, station, worker, subject, verb, manipulated object, target object, tool, timing, posture score and clip reference. This is the contract a vision and action model would fulfil on a plant's own cameras, and it matches how HA-ViD describes an action. `perception/nim_mode.py` shows where a hosted NVIDIA vision model plugs in; it is not exercised by the scripts.

**Procedure file.** `agent/procedure/assembly-box.json` lists the steps in order, the part and tool each needs, target seconds, thresholds, and the severity of each deviation type. The plant owns it. Upload a changed copy to `s3://<bucket>/procedure/` and the agent uses it on the next `analyze_pending`; no code changes.

**Rules.** `agent/rules.py` aligns each unit's actions with the steps by verb and part (order is stronger evidence than tool), then emits missed step, out of order, wrong tool, over time, posture strain and fatigue indicator deviations with the severities from the file. A unit is compliant when it has no deviation whose severity is listed under `compliance.counts_against_score`. Ambiguous sequences resolve toward wrong tool rather than out of order; see the docstring.

**Agent.** `main.py` starts `agent/server.py`, which serves MCP over streamable HTTP on port 8000 at `/mcp`, stateless, as AgentCore Runtime requires. It binds to loopback by default; the template sets `MCP_HOST` so the runtime listens on all interfaces inside its own isolated environment. The runtime uses direct code deployment: `deploy.sh` zips `main.py`, `agent/`, `perception/` and the dependencies from `agent/requirements.txt` (installed with `pip --platform manylinux2014_aarch64 --only-binary=:all:` so the compiled parts match the ARM64 runtime), uploads it to `code/agent-<hash>.zip`, and the template points `CodeConfiguration` at that key with `EntryPoint: [main.py]`. A new hash means a new key, which updates the runtime. It reads the findings on demand; there is no schedule and no copy of the data in Quick. Narratives come from Nemotron through the Bedrock Converse API with the computed facts as the only input.

**Auth.** AWS Quick calls MCP servers with OAuth, not SigV4, so the stack creates a Cognito user pool with a client-credentials app client, and the runtime validates those tokens (`CustomJWTAuthorizer`). Quick's connector uses Service-to-Service OAuth with that client. `seed.sh` and `status.sh` obtain a token the same way, so a green `status.sh` means the path Quick will use is open.

**Data flow in S3.** `actions/actions.csv` (perception output), `findings/findings.csv` (one row per unit), `deviations/deviations.csv` (one row per deviation), `state/processed_units.json`, `clips/` (raw video, expired after 30 days by lifecycle rule), `procedure/` (optional override), `planning/robot_capability.json` (optional override of the robot table).

**Planning.** `agent/planning/allocation.py` takes the measured worker seconds and strain events per step from the tables above and a robot table (cycle seconds and success rate per step), and compares ways to split the steps. The bundled robot table is a placeholder; `compare_task_allocation` says so in its result until you upload simulation results to `planning/robot_capability.json` with `"placeholder": false`.

## Data

The solution is designed around **HA-ViD** (Human Assembly Video Dataset, NeurIPS 2023): <https://iai-hrc.github.io/ha-vid>. People assemble a Generic Assembly Box of 35 parts on three plates with four standard tools. Each action is labeled with subject, action verb, manipulated object, target object and tool, and the labeling scheme is shared between humans and robots, which is why the same data can feed both the compliance view and the task-allocation view.

Read before using it:

- **License.** The project page states Creative Commons Attribution-NonCommercial 4.0. That allows research and evaluation with credit, and does not allow commercial use. This repository ships none of its data. Confirm with your legal team that your use fits.
- **What it does not contain.** Stations, shifts, calendar dates and posture scores. Those are plant context. You assign them in a small CSV, and results that depend on them (by shift, by station, fatigue, strain) are not HA-ViD measurements.
- **Format.** The authors' download format was not verified for this repository. `perception/havid_mode.py` therefore reads a neutral segments CSV (`video_id, start_frame, end_frame, subject, verb, object, target, tool`) that you export from the annotations, plus a context CSV (`video_id, worker, station, date, shift, start_time`) and an optional vocabulary JSON that maps dataset label names to the names in the procedure file.
- **Procedure.** `agent/procedure/assembly-box.json` follows the three-plate structure with generic part names. Align it with the dataset's label legend before reporting accuracy.

```bash
./seed.sh --havid-segments segments.csv --havid-context context.csv --havid-vocab vocab.json --fps 30
```

The bundled `data/sample_manifest.csv` is synthetic, generated by `data/generate_sample.py` with a fixed seed. Station ST-02 has the most missed steps, ST-03 the posture strain, and workers W-104 and W-107 slow down late in the shift so the fatigue rule fires. Use it to see every chart populated before plant data exists; do not quote its numbers as results.

## Tests and checks

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r scripts/requirements.txt pytest cfn-lint
LLM_ENABLED=false pytest -q            # rules, perception, pipeline, MCP server over HTTP, deploy flow, API shapes, code package
cfn-lint cfn/floor-compliance.yaml
```

The MCP server test starts `main.py` as a subprocess and calls every tool with the MCP client. The deploy-flow test runs `scripts/floor.py deploy` against fake AWS clients. The package test builds the real deployment zip and checks that every compiled wheel is aarch64. Request bodies for Quick Sight and Agent Registry are validated against the botocore service models.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Deploy says "Dashboard not built yet" | Expected on a first deployment: Quick Sight is not yet authorized for the new bucket (step 5). Also shown if no Quick user matches your sign-in | `./dashboard.sh --quicksight-user <arn>` (list users with `aws quicksight list-users --aws-account-id <id> --namespace default`) |
| `No AWS Region set` | CloudShell opened in a Region without a default | `./deploy.sh --region us-east-1` (use your Quick Region) |
| Stack fails because the bucket already exists | A previous deployment with the same project name | `./cleanup.sh`, or deploy with `--project <other-name>` |
| `pip could not fetch ARM64 wheels` | No PyPI access, or a dependency without a prebuilt aarch64 wheel | Check network access from CloudShell; keep `agent/requirements.txt` to packages with wheels |
| `AgentRuntime` fails to create or start | Code zip missing `main.py` at the root, wrong runtime version, or a dependency compiled for x86_64 | `./deploy.sh --rebuild`; the package test (`pytest tests/test_code_package.py`) checks the zip layout and wheel tags |
| Runtime starts but `status.sh` times out | Server not listening on all interfaces at port 8000, `/mcp` (the template sets `MCP_HOST` for this) | Check the runtime log group `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT` in CloudWatch |
| Registry record stays in `PENDING_APPROVAL` | Reused registry without auto-approval and your user lacks `UpdateRegistryRecordStatus` | Approve in the Agent Registry console, or ask the registry administrator |
| Registry card missing in Quick | Registry not in the same account and Region, uses JWT auth, or the record has no `remotes[].url` | Check with `aws agent-registry-control get-registry-record`; re-run `./deploy.sh` to rewrite the record |
| Connector authentication fails | A value was pasted with a missing character or extra space, or the sign-in domain is still starting | Run `./connector.sh` again and re-paste; wait a minute after deploy and retry |
| `status.sh` returns 403 | Token not accepted by the runtime | Confirm `QuickClientId` is in the runtime's `AllowedClients` (it is set by the template); redeploy if you changed the pool |
| Narrative says `[rules]` instead of `[model]` | Model not offered in the Region, or your identity lacks the AWS Marketplace permissions Bedrock needs to enable a model on first use | Read the "Model check" line in the deploy output; use a Region that offers the model or deploy with `--model-id` |
| Registry not listed in the Quick administration console | Administrator identity lacks `agent-registry:ListRegistries`, or the registry is not `READY`, not IAM-authorized, or in another Region | Add the permission; `./deploy.sh` creates a compliant registry in the stack's Region |
| Connectors page cannot load registry records | The Quick account uses a customer-managed service role | Add the three `agent-registry:*Discoverable*` read actions Quick displays to that role |
| Dashboard visuals show a permissions error | Quick Sight cannot read Athena or the bucket | Manage Quick Sight, Security & permissions: add Athena and the findings bucket |
| `pip` errors in CloudShell | System Python environment | The wrappers create `.venv`; delete `.venv` and re-run if it is damaged |

## Repository layout

```
main.py           AgentCore Runtime entry point
agent/            MCP server, rules engine, store, Bedrock wrapper, procedure file, planning (allocation and robot table), requirements
perception/       labels mode, HA-ViD converter and the NIM integration point
data/             sample manifest and its generator
cfn/              CloudFormation template
scripts/          floor.py (deploy, seed, status, dashboard, cleanup), Quick Sight definitions
quick/            chat agent instructions, dataset and dashboard definitions, embed guide
site/             supervisor page with embed slots
digital-twin/     command-line allocation wrapper and the simulation guide
tests/            pytest suite
deploy.sh status.sh connector.sh seed.sh dashboard.sh cleanup.sh   wrappers around scripts/floor.py
```

## Security

What the template sets up, and why:

- **Two S3 buckets.** The findings bucket holds the data. A second bucket receives the server access logs for both buckets. Both are encrypted, block public access, keep object versions, and reject any request that is not over TLS.
- **Old versions expire.** Noncurrent object versions are removed after 7 days and access logs after 90 days (`AccessLogRetentionDays`), so versioning does not grow storage without limit.
- **Connector secret.** The client credentials AWS Quick uses are stored in AWS Secrets Manager under a customer managed AWS KMS key with rotation on. That key costs about one US dollar a month while the stack exists.
- **Agent endpoint.** Inbound calls to the agent need a token from the stack's Amazon Cognito user pool.

There are no scanner suppressions in the code or the template. The access log bucket records access to itself under its own `log-bucket/` prefix, and the two `subprocess` calls pass only literal arguments with no shell.

`./cleanup.sh` turns off access logging, empties both buckets including old versions, and deletes the stack. The KMS key is scheduled for deletion with a 7-day waiting period, which is the shortest AWS allows.


See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for how to report issues. Worker video is personal data: the bucket is encrypted, blocks public access, denies non-TLS requests and expires raw clips after 30 days; the runtime role can read only this bucket; the front camera (faces) is not used by any rule.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
