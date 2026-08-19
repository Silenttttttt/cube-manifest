"""_private_git_dependency_warnings - a real incident, not a hypothetical:
an app's requirements.txt had an unpinned `git+https://...` dependency to a
private repo. Docker's build cache is keyed on that file's text, not the
remote's real content, so the layer that pip-installs it kept getting reused
even after the remote moved - the build reported success while silently
installing nothing. Separately, when the credential (`--build-secret`) was
missing, nothing noticed until something else finally forced that layer to
rebuild, at which point the clone failed with a plain git auth error days
later, nowhere near whatever had actually broken the credential."""

from __future__ import annotations

from pathlib import Path

from cube_manifest.build import _private_git_dependency_warnings


def _write_requirements(tmp_path: Path, text: str) -> Path:
    (tmp_path / "requirements.txt").write_text(text)
    return tmp_path


def test_no_requirements_file_means_no_warnings(tmp_path: Path):
    assert _private_git_dependency_warnings(tmp_path, None) == []


def test_requirements_with_no_git_dependency_is_silent(tmp_path: Path):
    _write_requirements(tmp_path, "flask\nrequests==2.31.0\n")
    assert _private_git_dependency_warnings(tmp_path, None) == []


def test_unpinned_git_dependency_with_no_secret_gets_both_warnings(tmp_path: Path):
    _write_requirements(tmp_path, "flask\ngit+https://github.com/org/private.git\n")
    warnings = _private_git_dependency_warnings(tmp_path, None)
    assert len(warnings) == 2
    assert any("no --build-secret" in w for w in warnings)
    assert any("unpinned" in w for w in warnings)


def test_unpinned_git_dependency_with_secret_only_warns_about_pinning(tmp_path: Path):
    _write_requirements(tmp_path, "git+https://github.com/org/private.git\n")
    warnings = _private_git_dependency_warnings(tmp_path, {"gh_pat": "/tmp/token"})
    assert len(warnings) == 1
    assert "unpinned" in warnings[0]


def test_pinned_git_dependency_with_no_secret_only_warns_about_the_secret(tmp_path: Path):
    _write_requirements(tmp_path, "git+https://github.com/org/private.git@abc1234\n")
    warnings = _private_git_dependency_warnings(tmp_path, None)
    assert len(warnings) == 1
    assert "no --build-secret" in warnings[0]


def test_pinned_git_dependency_with_secret_is_clean(tmp_path: Path):
    _write_requirements(tmp_path, "git+https://github.com/org/private.git@abc1234\n")
    assert _private_git_dependency_warnings(tmp_path, {"gh_pat": "/tmp/token"}) == []


def test_commented_out_git_line_is_ignored(tmp_path: Path):
    _write_requirements(tmp_path, "# git+https://github.com/org/private.git\nflask\n")
    assert _private_git_dependency_warnings(tmp_path, None) == []
