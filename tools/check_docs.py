"""Validate documentation links, examples, commands, diagrams and secret hygiene."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^```([\w-]*)\s*$")
INVOCATION = re.compile(
    r"(?:^\s*(?:\$ )?|uv run |\.venv/bin/|&& |\| )"
    r"(stram-[a-z][a-z-]*)(?![\w-]|\.(?:service|timer))",
    re.MULTILINE,
)
EXTERNAL_COMMANDS = {"stram-validate-read-only"}
MERMAID_STARTS = ("flowchart ", "sequenceDiagram", "stateDiagram", "stateDiagram-v2")
SECRETS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=[A-Za-z0-9+/_.-]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def markdown_files() -> list[Path]:
    names = (
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "ENGINEERING.md",
        "PLANS.md",
        "deploy/README.md",
    )
    paths = [ROOT / name for name in names]
    paths += sorted((ROOT / "docs").rglob("*.md"))
    return [path for path in paths if path.exists()]


def blocks(text: str) -> list[tuple[str, str, int]]:
    """Return language, body and first body line for each complete fenced block."""

    found: list[tuple[str, str, int]] = []
    language: str | None = None
    body: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match and language is None:
            language, body, start = match.group(1), [], number + 1
        elif line.strip() == "```" and language is not None:
            found.append((language, "\n".join(body), start))
            language = None
        elif language is not None:
            body.append(line)
    return found


def slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading.lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.strip().replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    result: set[str] = set()
    inside = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            inside = not inside
        match = None if inside else HEADING.match(line)
        if match:
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            result.add(base if count == 0 else f"{base}-{count}")
    return result


def fenced_lines(text: str) -> set[int]:
    return {
        number
        for _language, body, start in blocks(text)
        for number in range(start, start + body.count("\n") + 1)
    }


def check_links(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        text = path.read_text()
        fenced = fenced_lines(text)
        for number, line in enumerate(text.splitlines(), 1):
            if number in fenced:
                continue
            for target in LINK.findall(line):
                if re.match(r"^(https?:|mailto:)", target):
                    continue
                name, _, fragment = target.partition("#")
                destination = (path.parent / name).resolve() if name else path
                where = f"{path.relative_to(ROOT)}:{number}"
                if not destination.exists():
                    problems.append(f"{where}: missing link target {target}")
                elif (
                    fragment
                    and destination.suffix == ".md"
                    and fragment not in anchors(destination)
                ):
                    problems.append(f"{where}: missing anchor {target}")
    return problems


def check_commands(files: list[Path]) -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        known = set(tomllib.load(handle)["project"]["scripts"]) | EXTERNAL_COMMANDS
    problems: list[str] = []
    for path in files:
        for language, body, start in blocks(path.read_text()):
            if language not in {"bash", "sh", "console"}:
                continue
            for name in set(INVOCATION.findall(body)):
                if name not in known:
                    problems.append(f"{path.relative_to(ROOT)}:{start}: unknown command {name}")
    return problems


def check_secrets(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if any(pattern.search(line) for pattern in SECRETS):
                problems.append(f"{path.relative_to(ROOT)}:{number}: secret-shaped string")
    return problems


def check_examples(files: list[Path]) -> tuple[list[str], int]:
    problems: list[str] = []
    checked = 0
    for path in files:
        for language, body, start in blocks(path.read_text()):
            if language == "python":
                checked += 1
                try:
                    compile(body, f"{path}:{start}", "exec")
                except SyntaxError as error:
                    problems.append(f"{path.relative_to(ROOT)}:{start}: Python: {error.msg}")
            elif language in {"bash", "sh"}:
                checked += 1
                run = subprocess.run(
                    ["/bin/bash", "-n"], input=body, capture_output=True, text=True, check=False
                )
                if run.returncode:
                    detail = run.stderr.strip().splitlines()[-1]
                    problems.append(f"{path.relative_to(ROOT)}:{start}: shell: {detail}")
    return problems, checked


def check_mermaid(files: list[Path]) -> tuple[list[str], int]:
    problems: list[str] = []
    count = 0
    for path in files:
        for language, body, start in blocks(path.read_text()):
            if language != "mermaid":
                continue
            count += 1
            first = next((line.strip() for line in body.splitlines() if line.strip()), "")
            if not first.startswith(MERMAID_STARTS):
                problems.append(
                    f"{path.relative_to(ROOT)}:{start}: unsupported Mermaid declaration {first!r}"
                )
    if count == 0:
        problems.append("documentation contains no Mermaid diagrams")
    return problems, count


def main() -> int:
    files = markdown_files()
    examples, checked = check_examples(files)
    diagrams, diagram_count = check_mermaid(files)
    results = {
        "links": check_links(files),
        "commands": check_commands(files),
        "secrets": check_secrets(files),
        "examples": examples,
        "diagrams": diagrams,
    }
    for name, problems in results.items():
        print(f"{name}: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        for problem in problems:
            print(f"  {problem}")
    print(
        f"files: {len(files)}, examples syntax-checked: {checked}, "
        f"Mermaid diagrams: {diagram_count}"
    )
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
