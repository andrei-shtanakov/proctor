# Remote container workers (DOCKER_HOST=ssh://)

A docker fleet with `ssh_host` set runs its containers on a remote host by
pointing the container client at the remote socket over SSH. It reuses the
entire docker-worker lifecycle; only the socket is remote.

## Preconditions (the core's runtime must satisfy these)

`DOCKER_HOST=ssh://` runs the system `ssh` client from **inside the core's
runtime**. That runtime (host or the proctor image) must have:

- the `ssh` binary,
- a usable private key (ssh-agent or a mounted key),
- a `known_hosts` entry for each remote host,
- for **podman** remotes: a running `podman system service` (socket-activated)
  on the remote host — an installed binary is not enough.

Recommended per-host `~/.ssh/config` so a bad host key or dead host fails
fast instead of hanging (the code's `op_timeout` is the backstop):

    Host <remote>
        BatchMode yes
        ConnectTimeout 10
        StrictHostKeyChecking yes

## Config

`ssh_host` is `[user@]host[:port]` (no `ssh://` prefix — it is added
automatically). `nats_servers` must be a core address **routable from the
remote host** — `host.docker.internal`, `localhost`, `127.0.0.1`, `::1`,
and `172.17.0.1` are rejected because they never resolve to the core from
there.

## Known limitations

- A transport failure cannot be told apart from a container exit; the slot
  waits up to `max_unreachable_duration` before failing, rather than
  restarting immediately.
- A `run` killed by `op_timeout` that actually started the container on the
  remote host leaves an untracked container there; reap it manually.
- On shutdown, a fleet slot that failed as unreachable still issues
  stop+remove against the dead host, bounded by the op budgets
  (~`stop_timeout` + `op_margin`, then `op_timeout`) per replica,
  sequentially — teardown of an all-down remote fleet can take up to
  that much time per replica.
