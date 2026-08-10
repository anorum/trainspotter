# Self-hosted arm64 runners

Builds currently run on GitHub's x86 runners under QEMU, because native arm64
runners are free only on public repositories and this one is private.
Emulated builds work but take minutes where native takes seconds.

Running the runners on the cluster fixes that: the Pis *are* arm64, so builds are
native, consume no Actions minutes, and the runner reaches GitHub outbound only -
nothing is exposed inbound.

## Why this is safe here, and would not be on a public repo

A self-hosted runner executes whatever a workflow tells it to, on your network.
On a public repository a pull request from a fork can propose a workflow change
and have it run on your hardware. Private repositories have no such path: only
people with write access can trigger a run.

So the private/self-hosted pairing is the safe one, and it is worth stating
because the reverse combination is a well-known way to get a homelab owned.

## Install

Actions Runner Controller (ARC), which creates an ephemeral runner pod per job
and destroys it afterwards, so no build state carries between runs.

```sh
# 1. The controller
helm install arc \
  --namespace arc-systems --create-namespace \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller

# 2. A GitHub App or PAT for registration. A PAT needs `repo` scope.
kubectl create namespace arc-runners
kubectl create secret generic github-auth -n arc-runners \
  --from-literal=github_token='<PAT with repo scope>'

# 3. The runner scale set
helm install swagman-arm64 \
  --namespace arc-runners \
  --set githubConfigUrl="https://github.com/anorum/trainspotter" \
  --set githubConfigSecret=github-auth \
  --set minRunners=0 \
  --set maxRunners=2 \
  --set containerMode.type="dind" \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set
```

`minRunners=0` means nothing runs when idle - relevant on 8GB nodes.

## The container-build problem

A runner in Kubernetes has no Docker daemon. Two options:

- **`containerMode.type=dind`** (above) runs a Docker-in-Docker sidecar. Simple,
  well-trodden, and requires a **privileged** container.
- **Rootless BuildKit** needs no privileges. More setup, and the safer choice on
  a cluster that also runs household services.

dind is written above because it is the documented path, but privileged
containers on a home cluster deserve a deliberate decision rather than a default.
Prefer BuildKit if you would rather not grant it.

## Then

Change `runs-on` in `.github/workflows/images.yml`:

```yaml
runs-on: swagman-arm64        # was: ubuntu-latest
```

and drop the `setup-qemu-action` step plus `platforms: linux/arm64`, since the
runner is already arm64.

## Resource cost

Controller ~100Mi steady. Each runner pod ~500Mi-1Gi while a job runs, plus the
dind sidecar if used. With `maxRunners=2` that is up to ~2-3Gi during a build,
against roughly 6.7Gi free - so builds and Kafka should not be provisioned to
peak simultaneously.
