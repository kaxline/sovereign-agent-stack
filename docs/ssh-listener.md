# SSH listener

The SSH listener lets a client such as Boundary reach a host's SSH server when it can't open a TCP connection to port 22: a computer at home behind a router, or a VPS that keeps port 22 closed. The client connects to the listener by its HyperDHT public key, which punches through NAT over UDP and encrypts the stream end to end. The listener passes each stream to the host's sshd, so SSH sign-in, the host key check, commands, SFTP and port forwards work as they do over TCP.

The listener only carries bytes. sshd still checks the user's SSH key, and the client still checks the host key. The listener's own check comes first: it accepts streams only from client public keys on its allowed list, and a refused client never reaches sshd.

## Requirements

- Linux with Docker Engine, or macOS with Docker Desktop. Podman is not supported.
- bash and the Docker CLI, with the engine running.
- An SSH server on the host. On a Mac, turn on **Remote Login** in System Settings > General > Sharing.

The stack doesn't need to be installed. The listener runs apart from it, so a client can reach the host to install the stack, and stays connected while `scripts/install.sh` upgrades it.

## Set it up

Each release attaches `ssh-listener.sh` and its `.sha256`. Download both to the host, check the sum, and run:

```bash
bash ssh-listener.sh setup
```

Setup pulls the release's image, which is pinned by digest, makes the listener's key pair the first time, and starts the container `sovereign-ssh-listener` with restart policy `unless-stopped`. It prints the listener's public key and copies itself to `~/.local/bin/sovereign-ssh-listener` for the later commands. Enter the public key in Boundary. Boundary shows the `allow` command to run next, with its own client key:

```bash
~/.local/bin/sovereign-ssh-listener allow CLIENT_KEY --name laptop
```

Running a newer release's `ssh-listener.sh setup` moves the listener to that release's image. The key pair and the allowed list stay, so clients keep working. Setup waits up to 20 seconds for the new container to log that it is listening (`SSH_LISTENER_START_WAIT` changes the wait). If the container fails to start, or exits or restarts in that time, setup removes it, starts the previous image again and exits with `command_failed`. If it is still running but hasn't listened yet, which a slow DHT bootstrap can cause, setup keeps it and adds a warning to the status it prints.

### The sshd address

On Docker Engine the container uses host networking and passes streams to `127.0.0.1:22`. On Docker Desktop the container can't reach the Mac's loopback, so it uses `host.docker.internal:22`. `setup --sshd HOST:PORT` sets another address, and later setups keep it.

On a VPS, sshd can listen on loopback only (`ListenAddress 127.0.0.1` in `sshd_config`), which closes port 22 to the internet and leaves the listener as the only way in. Keep another way to reach the host, such as the provider's console, before you do this. sshd sees every connection through the listener as coming from `127.0.0.1` (on a Mac, from Docker Desktop's gateway), so `Match Address` rules and tools such as fail2ban that trust local addresses apply to every client.

## Manage client keys

```bash
sovereign-ssh-listener allow CLIENT_KEY [--name LABEL]
sovereign-ssh-listener revoke CLIENT_KEY
sovereign-ssh-listener list
```

A client key is 32 bytes written as 64 hex characters. The name is for you only. `allow` takes effect on the next connection attempt. `revoke` closes that key's open streams within a second, and its next attempt is refused. Neither restarts the listener. Both work while the listener is stopped.

## Status

```bash
sovereign-ssh-listener status
```

Status reports whether the listener is running, its public key, the sshd address and whether a TCP connection to it succeeds, the number of allowed keys, the version, and the NAT type HyperDHT sees for the host:

| NAT type | Meaning |
|---|---|
| `open` | Peers reach the host without hole punching. |
| `consistent` | The host is behind a NAT that keeps the same public port. Hole punching usually works. |
| `randomized` | The NAT picks a new port for each peer. A client also behind a randomizing NAT won't connect. |
| `unknown` | HyperDHT hasn't learned it yet, shortly after a start. |

It exits 0 whenever it can tell, including when the listener is stopped or not set up.

## Stop, start and remove

```bash
sovereign-ssh-listener stop
sovereign-ssh-listener start
sovereign-ssh-listener remove --yes
```

A stopped listener stays stopped after a reboot. `remove` deletes the container, its key pair and its allowed list, and the copied command. The stack keeps running. A later setup makes a new public key, so every client has to be set up again.

Run over the listener itself, `stop`, `remove` and `setup` cut the connection they arrived on. They finish anyway, but their output is lost; check with `status` once you are connected again.

## After a reboot

On Linux the listener starts with the Docker engine, so it answers on the same public key after a reboot, as long as the engine is enabled at boot (`systemctl enable docker`).

On a Mac, Docker Desktop runs only while a user is logged in, so the listener does too, like the stack. After a restart the host is unreachable until someone logs in. Setup warns when Docker Desktop is set not to start at login; turn on **Start Docker Desktop when you sign in** in its settings. With FileVault on, a Mac waits at the unlock screen after any restart, so for an update you start yourself, `sudo fdesetup authrestart` unlocks the disk once for the next boot. **Start up automatically after a power failure** in System Settings > Energy brings the Mac back on, but it still waits for a login.

A sleeping Mac is unreachable as well, since Docker Desktop's VM and the listener pause with it. On a Mac used as a host, turn on **Prevent automatic sleeping when the display is off** in System Settings > Energy.

## JSON output

Every command takes `--json` and prints one object:

- `setup`, `status`, `start` and `stop`: `{"running","setUp","publicKey","sshd","sshdReachable","allowed","version","natType","connections","warnings"}`. `setup` adds `"command"`, the path of the copied command. `warnings` lists what needs the user, such as Remote Login being off. `allowed` is null when the allowed list can't be read; the listener then refuses every client, and `warnings` says so.
- `allow`: `{"key","name","added","alreadyAllowed"}`.
- `revoke`: `{"key","revoked"}`. `revoked` is false when the key wasn't allowed.
- `list`: `{"keys":[{"key","name","added"}]}`.
- `remove`: `{"removed":true}`.

Errors print `{"error","message"}`: `bad_input` (exit 2), `command_failed` (exit 4), `unsupported_host` and `not_set_up` (exit 1).

## From a checkout

`scripts/ssh-listener.sh` in the repository has no image pinned. `make ssh-listener-image` builds `sovereign-ssh-listener:local`; run the script with `SSH_LISTENER_IMAGE=sovereign-ssh-listener:local`. `make ssh-listener-test` runs the listener's tests against a local HyperDHT testnet in the image's test stage, including an `ssh2` client signing in to a real OpenSSH sshd through the listener (host key check, a command, SFTP, a port forward, and revoke ending the session). The source is in `compose/ssh-listener/`.
