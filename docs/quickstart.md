# Run your first GPU job

Gipper needs a Linux client, Python 3.12+, OpenSSH, and tar with gzip support.
Provider accounts supply the GPUs; a missing API key disables that provider for
rentals. Your application and dependencies remain part of your job.

## Install from source

```sh
git clone https://github.com/lavallee/gipper.git
cd gipper
uv tool install .
gipper --help
```

Alternatively, use `pip install .` in a virtual environment. Install uv separately
if using the uv commands. The Python runtime has no third-party dependencies.

## Configure a provider

```sh
mkdir -p ~/.config/gipper
(umask 077; cp -n .env.example ~/.config/gipper/.env)
chmod 600 ~/.config/gipper/.env
```

Edit that file to set `VAST_API_KEY` or `RUNPOD_API_KEY`. Environment variables
override dotenv values. An explicit `GIPPER_ENV` selects a different file.
Never put API keys in a job, shell command argument, or issue report.
Shadeform is experimental and has not had live qualification.

## Inspect before renting

```sh
gipper pod estimate examples/smoke.toml --provider vast
gipper pod run examples/smoke.toml --provider vast --dry-run
```

These commands contact provider APIs but do not rent. Read the estimated phases,
fees, and assumptions. The smoke job requests at least 16 GB of GPU memory and
limits estimated spend to $0.10 and runtime to 15 minutes. Those are client-side
safeguards; provider rounding or failed cleanup can make the bill larger.

## Run and verify closure

The next command spends money. Run it only when you intend to rent:

```sh
gipper pod run examples/smoke.toml --provider vast
gipper pod ls
```

The example uploads a text file, runs `nvidia-smi`, and collects results in
`examples/smoke-out/`. It checks the transfer and lifecycle, not GPU performance.
Check the provider console if a machine remains listed or closure is uncertain.

## Keep cleanup available

```sh
mkdir -p ~/.config/systemd/user
cp deploy/gipper-reap.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gipper-reap.timer
```

The service expects `~/.local/bin/gipper`; adjust it if your installation lives
elsewhere. Keep provider credentials in the user-config dotenv so the service
can read them. Timers need a running user manager; user lingering or another
independent scheduler is needed to cover logout. A remote command timeout does
not terminate the rental.

## Bring your own workload

Copy the smoke job and change `[run] command`, `[run] setup`, `[[inputs]]`, and
`[[outputs]]`. Set GPU requirements, budgets, and placement hints for your
training, inference, renderer, or other shell-driven application. Use relative
input paths so jobs remain portable. Repeated comparable jobs should share a
`workload` name; use a separate name or explicit estimates when work changes.

See [placement](placement.md), the [Python API](clients.md), and the
[security model](../SECURITY.md) before running sensitive or long-lived work.
