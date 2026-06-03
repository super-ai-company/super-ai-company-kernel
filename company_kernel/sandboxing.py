from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "sandbox_profiles.json"


def load_profiles(path: Path = DEFAULT_CONFIG) -> dict:
    if not path.exists():
        return {"profiles": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def profile_for(runtime: str, profile: str = "default", config: dict | None = None) -> dict:
    data = config or load_profiles()
    runtime_profiles = data.get("profiles", {}).get(runtime, {})
    result = runtime_profiles.get(profile) or runtime_profiles.get("default")
    if not result:
        return {"isolation": "none", "network": "default", "writable_paths": [], "readonly_paths": []}
    return result


def docker_command(base_cmd: list[str], workspace: Path, profile: dict) -> list[str]:
    image = profile.get("image", "python:3.12-slim")
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none" if profile.get("network") == "none" else str(profile.get("network", "bridge")),
        "-v",
        f"{workspace}:{workspace}:rw",
        "-w",
        str(workspace),
    ]
    for path in profile.get("readonly_paths", []):
        host_path = Path(path).expanduser()
        cmd.extend(["-v", f"{host_path}:{host_path}:ro"])
    for path in profile.get("writable_paths", []):
        host_path = Path(path).expanduser()
        if host_path == workspace:
            continue
        cmd.extend(["-v", f"{host_path}:{host_path}:rw"])
    cmd.append(image)
    cmd.extend(base_cmd)
    return cmd


def firejail_command(base_cmd: list[str], workspace: Path, profile: dict) -> list[str]:
    cmd = ["firejail", "--quiet", "--private=" + str(workspace)]
    if profile.get("network") == "none":
        cmd.append("--net=none")
    for path in profile.get("readonly_paths", []):
        cmd.append("--read-only=" + str(Path(path).expanduser()))
    return [*cmd, *base_cmd]


def wrap_command(base_cmd: list[str], *, runtime: str, workspace: Path, isolation: str = "none", profile_name: str = "default", config: dict | None = None) -> list[str]:
    if isolation in {"", "none", "local"}:
        return base_cmd
    profile = profile_for(runtime, profile_name, config)
    profile = {**profile, "isolation": isolation}
    if isolation == "docker":
        return docker_command(base_cmd, workspace, profile)
    if isolation == "firejail":
        return firejail_command(base_cmd, workspace, profile)
    raise ValueError(f"unsupported isolation: {isolation}")
