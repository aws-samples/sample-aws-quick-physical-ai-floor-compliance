# Digital twin and VLA extension (whitepaper section 5)

This folder holds the guide for the simulation behind human-robot task allocation. The calculator itself lives in `agent/planning/` and is deployed with the agent, so the manager can ask allocation questions in AWS Quick from day one. Only the simulation that produces real robot numbers needs GPU capacity, and it is not part of `./deploy.sh`.

## What is here

| File | Purpose |
| --- | --- |
| `allocation.py` | Command-line wrapper around `agent/planning/allocation.py`: units per hour, worker busy and waiting time, strain remaining and expected retries per scenario. Deterministic; tested in `tests/test_allocation.py`. |
| `../agent/planning/robot_capability.json` | The robot table. **The bundled numbers are placeholders.** |

```bash
python digital-twin/allocation.py \
  --actions data-local/actions/actions.csv \
  --deviations data-local/deviations/deviations.csv
```

## How the robot table is produced

The robot numbers come from NVIDIA Isaac Sim, not from this repository:

1. **Build the station scene** in Isaac Sim (or Omniverse): bench, parts, tools, a cobot arm from its URDF or CAD. NVIDIA's Isaac Lab manipulation tasks are a reasonable starting point for pick, place and fastening.
2. **Run a VLA policy** (for example NVIDIA Isaac GR00T) on each candidate robot step with a language instruction such as "fasten the shaft nut". Record cycle time and success over a few hundred episodes per step.
3. **Write the results** into a JSON with the shape of `agent/planning/robot_capability.json`, set `"placeholder": false` and name the source (model, scene, episode count, date).
4. **Upload it** to `s3://<bucket>/planning/robot_capability.json`. The agent uses it on the next question and stops flagging the figures as placeholders.

NVIDIA Isaac Lab includes contact-rich assembly tasks (peg insertion, gear meshing, nut threading) that are close to the box's insert and fasten steps and are a reasonable starting scene. HA-ViD's human-robot shared labels mean a robot step in simulation and a worker step on video carry the same name.

## Infrastructure

- Isaac Sim on AWS: the NVIDIA documentation covers a Marketplace AMI on a `g6e.2xlarge` (L40S) instance, or the `isaac-sim` container on a Linux GPU instance in headless mode.
- GR00T and Isaac Lab need the same class of GPU instance; budget hours for policy evaluation.
- None of this runs inside AWS Quick. Quick is the interface: the floor compliance agent exposes `get_step_profile` and `compare_task_allocation` over the same connector, and Quick renders the comparison.

## Scope boundary

The output is a plan and a validated simulation. Moving a cobot to the floor needs robot demonstration data for the chosen steps, edge GPU hardware at the station, and collaborative-robot safety certification (ISO 10218 and ISO/TS 15066 are the usual references). Label every number from this folder as simulated until that work is done.
