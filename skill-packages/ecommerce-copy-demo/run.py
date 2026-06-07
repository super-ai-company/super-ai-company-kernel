from __future__ import annotations

import os
from pathlib import Path


task_id = os.environ.get("TASK_ID", "unknown-task")
skill_id = os.environ.get("SKILL_ID", "ecommerce-copy-demo")
workspace = Path(os.environ["TASK_WORKSPACE"])
final_dir = workspace / "final"
final_dir.mkdir(parents=True, exist_ok=True)
out = final_dir / "listing-summary.md"
out.write_text(
    "\n".join(
        [
            "# Ecommerce Listing Summary",
            "",
            f"- task_id: `{task_id}`",
            f"- skill_id: `{skill_id}`",
            "- title: Lightweight product listing package",
            "- bullets: fast setup; clear artifact; promotable evidence",
            "- next_step: replace this demo script with a real seller workflow.",
            "",
        ]
    ),
    encoding="utf-8",
)
