"""Entry point: `generate_dockerfile(app, app_dir=None) -> str`.

Two paths:
  1. docker_config.dockerfile is set - the app brings its own handwritten
     Dockerfile (local-storage, voice-cloning, video-generator, ...). We
     don't generate anything; we validate the file exists relative to the
     app's own directory/context and return its content untouched. Forcing
     generation onto an app that deliberately opted out would silently
     discard whatever that Dockerfile actually does.
  2. Otherwise, dispatch to the per-language two-stage builder in
     languages.py, defaulting to the generic builder for any language value
     the table doesn't recognize (e.g. the real `language: docker` app in
     apps/*/app.yml) rather than raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from cube_manifest.schema.models import AppConfig

from .languages import LANGUAGE_BUILDERS, build_generic, build_python

__all__ = ["generate_dockerfile"]


def _resolve_passthrough(app: AppConfig, app_dir: Path | None) -> str:
    dc = app.docker_config
    assert dc.dockerfile is not None
    if app_dir is None:
        raise ValueError(
            f"{app.name}: docker_config.dockerfile={dc.dockerfile!r} is set but no app_dir "
            "was given to resolve it against"
        )
    context_dir = app_dir / (dc.context or ".")
    dockerfile_path = context_dir / dc.dockerfile
    if not dockerfile_path.is_file():
        raise FileNotFoundError(
            f"{app.name}: docker_config.dockerfile={dc.dockerfile!r} not found at "
            f"{dockerfile_path} (context={dc.context or '.'!r}, app_dir={app_dir})"
        )
    return dockerfile_path.read_text()


def generate_dockerfile(app: AppConfig, app_dir: Path | None = None) -> str:
    dc = app.docker_config
    if dc.dockerfile is not None:
        return _resolve_passthrough(app, app_dir)

    if dc.language == "python":
        return build_python(app, app_dir)

    builder = LANGUAGE_BUILDERS.get(dc.language, build_generic)
    return builder(app)
