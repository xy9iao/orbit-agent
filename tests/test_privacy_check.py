"""scripts/privacy_check.sh: CJK terms match as substrings, other terms as whole words, in every mode."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import ROOT

SCRIPT = ROOT / "scripts" / "privacy_check.sh"
# A CJK term must also match inside a longer CJK phrase (DISC + 突出). A neutral stand-in: never put a real
# private term in a tracked file, even escaped.
DISC = "甲乙丙"


def find_bash() -> str | None:
    """Git's bash on Windows (System32\bash.exe would be WSL), else bash on PATH."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for candidate in (Path(git).parent.parent / "bin" / "bash.exe", Path(git).parent / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


BASH = find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")
ENV = {
    **os.environ,
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
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"README.md": "Orbit\n"}, "Init")
    return repo


def check(repo: Path, terms: list[str], *args: str) -> tuple[int, str]:
    terms_file = repo.parent / "terms.txt"
    terms_file.write_text("# private\n" + "\n".join(terms) + "\n", encoding="utf-8")
    result = subprocess.run([BASH, str(SCRIPT), *args], cwd=repo, capture_output=True, timeout=60,
                            env={**ENV, "PRIVACY_TERMS_FILE": str(terms_file)})
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace")


@pytest.mark.parametrize("mode", ["tree", "--staged", "--history"])
def test_cjk_term_matches_inside_a_longer_cjk_phrase(repo, mode):
    (repo / "notes.md").write_bytes(f"诊断：{DISC}突出，需休息\n".encode())
    git(repo, "add", "notes.md")
    if mode != "--staged":
        git(repo, "commit", "-q", "-m", "notes")

    code, output = check(repo, [DISC], *([] if mode == "tree" else [mode]))

    assert code == 1, output
    assert f"{DISC}突出" in output and "FAILED - 1 line(s)" in output


@pytest.mark.parametrize("mode", ["tree", "--history"])
def test_ascii_term_matches_whole_words_only(repo, mode):
    commit(repo, {"export.py": "PGABCWORD = 1\n", "notes.md": "grade: ABC\n"}, "add")

    code, output = check(repo, ["ABC"], *([] if mode == "tree" else [mode]))

    assert code == 1, output
    assert "grade: ABC" in output and "PGABCWORD" not in output


def test_a_line_matching_both_kinds_of_terms_is_listed_once(repo):
    commit(repo, {"notes.md": f"ABC {DISC}突出\n"}, "add")

    code, output = check(repo, ["ABC", DISC])

    assert code == 1
    assert output.count("notes.md:1:") == 1 and "FAILED - 1 line(s)" in output


def test_clean_repo_passes(repo):
    commit(repo, {"export.py": "PGABCWORD = 1\n"}, "add")

    code, output = check(repo, ["ABC", DISC], "--history")

    assert code == 0, output
    assert "privacy check: ok (2 terms, --history)" in output
