"""Inferring the repository from a window title.

A window title is the only thing an editor exposes to the outside world,
so these tests use the real title formats of the editors people use.
"""

from __future__ import annotations

import subprocess

import pytest

from core import git_probe


@pytest.mark.parametrize(
    "title,expected",
    [
        ("main.py - my-project - Visual Studio Code", "my-project"),
        # The bullet marks unsaved changes in VS Code.
        ("● main.py - my-project - Visual Studio Code", "my-project"),
        ("* main.py - my-project - Visual Studio Code", "my-project"),
        ("app.tsx - my-project - Cursor", "my-project"),
        ("akmal@DESKTOP: ~/my-project", "my-project"),
        ("my-project [SSH: devbox]", "my-project"),
    ],
)
def test_workspace_name_is_found(title, expected):
    assert expected in git_probe.candidate_names(title)


def test_empty_title_yields_nothing():
    assert git_probe.candidate_names("") == []


def test_absolute_path_in_title_is_extracted(tmp_path):
    title = f"{tmp_path} - Windows Terminal"
    assert git_probe.path_in_title(title) == tmp_path


def test_file_path_in_title_resolves_to_its_folder(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("print()", encoding="utf-8")
    assert git_probe.path_in_title(str(target)) == tmp_path


def test_no_path_in_a_plain_title():
    assert git_probe.path_in_title("Inbox - Outlook") is None


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.invalid"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    return path


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "demo-repo")


def test_find_repo_root_walks_upwards(repo):
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert git_probe.find_repo_root(nested) == repo


def test_find_repo_root_returns_none_outside_a_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert git_probe.find_repo_root(plain) is None


def test_branch_is_read_without_running_git(repo):
    branch, detached = git_probe.read_branch(repo)
    assert branch == "trunk"
    assert detached is False


def test_branch_of_a_non_repo_is_none(tmp_path):
    assert git_probe.read_branch(tmp_path) == (None, False)


def test_resolve_repo_finds_project_under_a_root(repo):
    title = "main.py - demo-repo - Visual Studio Code"
    assert git_probe.resolve_repo(title, [repo.parent]) == repo


def test_resolve_repo_returns_none_when_absent(tmp_path):
    title = "main.py - nothing-here - Visual Studio Code"
    assert git_probe.resolve_repo(title, [tmp_path]) is None


def test_dirty_count_sees_a_new_file(repo):
    git_probe._status_cache.clear()
    assert git_probe.count_dirty(repo) == 0
    (repo / "new.txt").write_text("hello", encoding="utf-8")
    git_probe._status_cache.clear()
    assert git_probe.count_dirty(repo) == 1


def test_describe_pluralises(tmp_path):
    state = git_probe.RepoState(tmp_path, "main", 1)
    assert git_probe.describe(state) == "main · 1 change"
    state = git_probe.RepoState(tmp_path, "main", 3)
    assert git_probe.describe(state) == "main · 3 changes"
    state = git_probe.RepoState(tmp_path, "main", 0)
    assert git_probe.describe(state) == "main · clean"


def test_describe_without_git_shows_branch_only(tmp_path):
    state = git_probe.RepoState(tmp_path, "main", None)
    assert git_probe.describe(state) == "main"
