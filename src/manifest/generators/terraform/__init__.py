"""`.tf.json` Terraform generator - builds plain Python dicts from a
validated `AppConfig` and serializes them with `json.dumps()`, replacing the
old `terraform_generator.py`'s f-string-interpolated-HCL-text approach (a
confirmed HCL-injection vulnerability: a crafted app.yml value could inject
arbitrary HCL, e.g. a `local-exec` provisioner). See `builder.generate_terraform`
for the entry point.
"""

from __future__ import annotations

from .builder import generate_terraform, render

__all__ = ["generate_terraform", "render"]
