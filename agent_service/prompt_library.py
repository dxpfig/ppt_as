"""Read one immutable snapshot of the editable PPT prompt library per job."""
import hashlib
from pathlib import Path

PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "ppt"
SCENARIOS = ("general", "management-report", "analysis-decision", "business-plan", "tech-engineering", "academic-research", "education-training", "brand-creative")


class PromptLibrary:
    def __init__(self, root=None):
        root = Path(root or PROMPT_ROOT)
        names = ["common", "layouts", "planner", "writer", "editor", "repair", "scenarios/index"]
        names += [f"scenarios/{name}" for name in SCENARIOS]
        self.files = {}
        for name in names:
            try:
                text = (root / f"{name}.md").read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"PPT 提示词文件不可读：{name}.md") from exc
            if not text:
                raise ValueError(f"PPT 提示词文件为空：{name}.md")
            self.files[name] = text
        self.version = hashlib.sha256("\n".join(f"{n}\n{self.files[n]}" for n in sorted(self.files)).encode()).hexdigest()[:16]

    def system(self, role, scenario="general"):
        if role not in ("planner", "writer", "editor", "repair") or scenario not in SCENARIOS:
            raise ValueError("未知的 PPT 提示词角色或场景。")
        return "\n\n".join(self.files[name] for name in ("common", "layouts", "scenarios/index" if role == "planner" else f"scenarios/{scenario}", role))
