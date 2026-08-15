# cube-manifest

One declarative `app.yml` per app → a generated Dockerfile, Terraform, and
a real deploy to your own Kubernetes cluster. No Helm chart to hand-write,
no separate CI workflow to maintain by hand, no bespoke build script.

```yaml
# apps/hello/app.yml
name: hello
enabled: true
app_type: service
port: 8080

docker_config:
  language: python
  entry_point: ["python", "app.py"]

scaling:
  min_replicas: 0          # scale to zero when idle
  idle_timeout_seconds: 300
  activation:
    type: http
```

```bash
cube generate dockerfile hello   # see the Dockerfile it would build
cube generate terraform hello    # see the .tf.json it would apply
cube build hello                 # real docker build + push to your registry
cube plan hello                  # real `terraform plan` against your cluster, read-only
cube apply hello --yes           # actually apply it
cube ship hello --yes            # build + apply + rollout-restart, all in one - the common case
                                  # for shipping a real code change to an already-deployed app
```

## Why this exists

Kubernetes has plenty of tools for pieces of this (Helm templates
manifests, Terraform manages infrastructure, GitHub Actions builds
images) but wiring them together for a small self-hosted cluster still
means hand-maintaining a Dockerfile, a Terraform module, a CI workflow,
and your own scale-to-zero story separately, per app. cube-manifest is
the opposite bet: one file describes the app, and everything else is
generated from it - closer to Railway/Fly.io's "one config, one deploy"
model, but for a cluster you actually own and can inspect every
generated artifact of.

This isn't a general-purpose Kubernetes templating tool (that's Helm's
job) or a multi-cloud IaC platform (that's Terraform/Pulumi's job) - it's
opinionated on purpose. If you outgrow the opinion, the generated
Dockerfile/Terraform are plain, inspectable text you own outright, not
hidden behind a DSL.

```mermaid
flowchart LR
    A["app.yml"] --> B["Schema<br/>(Pydantic validation)"]
    B --> C["Dockerfile generator"]
    B --> D["Terraform generator<br/>(.tf.json)"]
    C --> E["docker build + push"]
    D --> F["cube plan<br/>(read-only)"]
    E --> G{"cube apply --yes"}
    F --> G
    G --> H[("Live Kubernetes cluster")]
    H -. "annotations" .-> I["Activator<br/>(scale-to-zero 0↔1)"]
    I -. "proxies traffic" .-> H
```

## What's real right now

- A single, fully validated `app.yml` schema (Pydantic) - one canonical
  shape for health checks, security context, storage, RBAC, scheduling,
  scale-to-zero activation, everything. No silent typos: unknown fields
  fail loudly at `cube validate` instead of being ignored.
- A Terraform generator that builds plain Python dicts and serializes them
  as `.tf.json` - not hand-assembled HCL strings. There's no string-
  templating step for a value to escape out of, which is the actual point:
  a value containing `"`, `${...}`, or literal HCL syntax is just an inert
  string leaf, never new structure.
- A Dockerfile generator that's mandatory two-stage for every language
  (python/rust/node/go/java, plus passthrough if you bring your own
  Dockerfile) and uses BuildKit secret mounts for anything like a private
  git deploy key - it never ends up baked into a layer, builder or final.
- Real `cube apply`: checks what already exists live via `kubectl get`,
  imports it into a throwaway local Terraform state first (import only
  adds state tracking, it can't mutate or delete anything), *then* plans
  and applies - so re-running this against an app someone else's tool
  already deployed doesn't blow up with "already exists," and never
  guesses a resource's kind from its name like naive approaches do.
  `apply` always shows a real plan first; nothing happens without `--yes`.

  ```mermaid
  sequenceDiagram
      participant You
      participant cube as cube apply
      participant kubectl as kubectl (read-only)
      participant terraform
      participant Cluster as Live cluster

      You->>cube: cube apply myapp --yes
      cube->>cube: generate .tf.json
      loop for each resource
          cube->>kubectl: get <kind> <name>
          kubectl->>Cluster: read-only check
          alt already exists
              cube->>terraform: import (state only, no mutation)
          else doesn't exist
              Note over cube: leave un-imported - real create
          end
      end
      cube->>terraform: plan -out=tfplan
      terraform-->>You: show the real diff
      cube->>terraform: apply tfplan
      terraform->>Cluster: apply for real
  ```
- Scale-to-zero integration: emits the annotation contract a separate
  always-on "activator" process reads to do real 0↔1 scaling - covered by
  a contract test that round-trips through that process's actual parser,
  not just a hand-maintained assumption of what it expects.
- A Fernet decrypt bridge for secrets already encrypted with the
  `ENC[...]` scheme some clusters use - decryption happens once, at load
  time; nothing downstream needs to know the format exists, and nothing is
  ever written to disk in plaintext.
- Node image prewarming after `cube build`: `build_and_push` only ever
  talks to the registry via plain `docker push` - no node's kubelet ever
  pulls the image until some Deployment actually needs it. For a
  `min_replicas: 0` app that first pull gets deferred all the way to the
  next real cold start, which then pays full network-pull latency as part
  of what's supposed to be a fast scale-up. Confirmed for real on this
  project's own cluster: a 55MB image's first-ever pull took 94s
  immediately after a build, even though a raw registry blob fetch on the
  same link measured over 1GB/s moments later - the image was simply never
  resident on any node yet, not a slow registry. `cube build` now forces
  every Ready, schedulable node to pull the freshly pushed image right
  away (a disposable per-node Pod, `imagePullPolicy: Always`, deleted once
  the pull completes) - the same 55MB image's *next* cold start pulled in
  61ms instead of 94s. `--no-prewarm` skips this if you'd rather defer the
  pull yourself.

All of the above has been run for real, not just tested in isolation -
see [Battle-tested](#battle-tested) below.

## What's not built yet

- A plugin system for anything beyond the built-in language/resource
  support (no third-party hooks yet).
- A real secrets backend (sops/age) - the Fernet bridge is a compatibility
  shim for an existing format, not the intended long-term design.
- Storage class / ingress class cluster-wide defaults in the cluster-config
  file - only `registry_url` lives there so far (see `cube build`/`cube
  generate terraform` below); those two are still read from each app's own
  `app.yml`.
- `external_repo` build caching - `cube build` shallow-clones fresh every
  time rather than keeping a persistent mirror per repo URL.

## Cluster config

Registry URL (and, later, other cluster-specific defaults) live in an
optional `.cube-manifest.yaml`, discovered by walking up from `--apps-dir`'s
parent directory the same way `.git`/`.eslintrc` get found:

```yaml
registry_url: "my-registry.example:5000"
```

`CUBE_MANIFEST_REGISTRY_URL` overrides whatever the file says. With neither
a file nor the env var, `registry_url` defaults to `localhost:5000` - a
generic default, not any one deployment's real value.

## Install

```bash
git clone https://github.com/Silenttttttt/cube-manifest
cd cube-manifest
uv sync --extra dev   # or: pip install -e ".[dev]"
```

Requires `terraform` and `kubectl` on `PATH`, and a working kubeconfig.

## CLI

```
cube list                          # every app under ./apps, type + enabled state
cube validate [app...]             # schema-check one, several, or all apps
cube generate dockerfile <app>     # print (or --out FILE) the generated Dockerfile
cube generate terraform <app>      # print (or --out FILE) the generated .tf.json
cube build <app> [--no-push] [--no-prewarm]  # real docker build, tag as :latest, roll the old :latest
                                    # to :previous first, then push (unless --no-push) and prewarm
                                    # every node's image cache (unless --no-prewarm)
cube plan <app>                    # real terraform plan against your cluster - read-only
cube apply <app> [--yes]           # apply it for real - requires --yes to actually touch anything
cube ship <app> [--yes] [--no-restart] [--no-push] [--no-prewarm]
                                    # build + apply + rollout-restart, composed into one command -
                                    # the common case of actually shipping a real code change to an
                                    # already-deployed app (reapplying an unchanged :latest tag never
                                    # forces already-running pods to repull it, so a bare `apply` on
                                    # its own isn't enough after a code change). build/plan/apply stay
                                    # separate, independently-useful primitives - `ship` doesn't
                                    # replace them, it's just those three run back-to-back. Same --yes
                                    # gate as `apply`: without it, still builds+pushes for real (like
                                    # `build` always does) and shows the plan, but never applies or
                                    # restarts. --no-restart for a config-only change with no new image.
```

All commands take `--apps-dir` to point at wherever your `apps/<name>/app.yml`
files live.

## Battle-tested

This isn't a toy - it replaced the deploy pipeline for a real homelab
Kubernetes cluster's actual running services. Every one of the following
was verified against the live cluster, not just asserted by a test suite:

- A real `terraform plan`/`apply` cycle against a live cluster, including
  importing pre-existing resources so the diff is the real diff, not
  false "will create" noise.
- A real cold-start through a scale-to-zero activator, end to end, for
  multiple apps.
- A real message published through a message broker using credentials
  decrypted by the Fernet bridge - proving the decrypted value matched
  the live broker's actual configured credentials exactly.
- A real `psql` query against a database after cutover, confirming every
  database was still there.
- A zero-downtime rolling update of the activator process itself (2
  replicas, watched pod-by-pod through the whole rollout), followed by
  forcing a different app back to zero replicas and confirming a fresh
  cold-start still worked - proving the tool didn't break the very thing
  it depends on to test itself.
- Three real bugs were caught by this process *before* they could cause
  damage: a missing namespace label that would have been silently
  stripped, an Ingress TLS default that would have dropped a real
  certificate, and a resource-type mapping typo that would have made
  Terraform try to create a DaemonSet that already existed.

## License

MIT — see [LICENSE](LICENSE).
