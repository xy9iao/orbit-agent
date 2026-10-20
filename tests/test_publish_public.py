"""scripts/publish_public.sh --dry-run on a tiny fixture repo: private paths and terms leave the whole history."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ROOT

SCRIPT = ROOT / "scripts" / "publish_public.sh"
TERMS = ["XTERM-42", "ABC", "保密词"]


def find_bash() -> str | None:
    """Git's bash on Windows (System32\\bash.exe would be WSL), else bash on PATH."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for candidate in (Path(git).parent.parent / "bin" / "bash.exe", Path(git).parent / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


BASH = find_bash()
pytestmark = [
    pytest.mark.skipif(BASH is None, reason="needs bash"),
    pytest.mark.skipif(importlib.util.find_spec("git_filter_repo") is None, reason="needs git-filter-repo"),
]
ENV = {
    **os.environ,
    # git-filter-repo is installed next to this Python (the venv's Scripts/ or bin/).
    "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "LC_ALL": "C",
}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], env=ENV, capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="replace")


def commit(repo: Path, files: dict[str, str | None], message: str) -> None:
    for name, content in files.items():
        path = repo / name
        if content is None:
            git(repo, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def private_repo(tmp_path: Path) -> Path:
    """Three commits; the private details are only in the first one."""
    repo = tmp_path / "private"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {
        "notes.md": "Diagnosis XTERM-42, grade ABC 3.9, 一个保密词在这里.\n",  # a CJK term inside CJK text
        "code.py": "PGABCWORD = 1  # ABC inside a longer word must survive\n",
        "packs/fitness/SKILL.md": "private training plan\n",
        "private-packs/study/SKILL.md": "private study plan\n",
        "QUESTIONS.md": "owner to-do\n",
        ".publicignore": "# owner notes\nQUESTIONS.md\n",
    }, "Add notes about XTERM-42")
    commit(repo, {"notes.md": "Project notes.\n", "packs/fitness/SKILL.md": None}, "Clean notes")
    commit(repo, {"README.md": "Orbit\n"}, "Add README")
    return repo


def run_publish(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([BASH, str(SCRIPT), *args], env=ENV, capture_output=True, timeout=300)


def test_dry_run_removes_private_terms_and_paths_from_the_whole_history(private_repo, tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text("# private\n" + "\n".join(TERMS) + "\n", encoding="utf-8")
    work = tmp_path / "work"

    result = run_publish("v1.0", "--dry-run", "--source", str(private_repo), "--terms", str(terms),
                         "--workdir", str(work), "--remote", str(tmp_path / "never-used.git"))

    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert "privacy check: ok" in output and "dry run, nothing pushed" in output
    assert "commits:" in output and "files:" in output

    public = work / "repo"
    history = git(public, "log", "-p", "--all", "--format=%H%n%B")
    for term in TERMS:
        assert term not in history.replace("PGABCWORD", "")
    assert "[redacted]" in history
    assert "PGABCWORD" in history  # whole-word replacement: code that merely contains a term is untouched
    assert "Add notes about [redacted]" in history

    paths = set(git(public, "log", "--all", "--format=", "--name-only").split())
    assert not any(p.startswith(("packs/fitness/", "private-packs/")) or p == "QUESTIONS.md" for p in paths)
    assert {"notes.md", "code.py", "README.md"} <= paths
    assert git(public, "tag", "--list").split() == ["v1.0"]
    assert git(public, "remote").strip() == ""  # no way back to the private origin
    assert not (private_repo / ".git" / "filter-repo").exists()  # the private repo itself is untouched
    assert "XTERM-42" in git(private_repo, "log", "-p", "--all")


def test_refuses_to_run_without_a_terms_file(private_repo, tmp_path):
    result = run_publish("v1.0", "--dry-run", "--source", str(private_repo), "--terms", str(tmp_path / "none"),
                         "--workdir", str(tmp_path / "work"))

    assert result.returncode != 0
    assert b"refusing to publish without the privacy terms" in result.stderr
    assert not (tmp_path / "work").exists()
