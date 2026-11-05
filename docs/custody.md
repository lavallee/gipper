# Custody: what reached each machine

When a provider tells you a machine hosting your work may have been compromised, you
need three answers quickly: what was on it, which credentials to rotate, and what you
can't be sure about. gipper keeps that trail as it works.

```
gipper custody report --provider runpod --since 2026-10-01 --check-providers
gipper custody show POD_ID                # every file sent to one machine
gipper custody find ./data/private.csv    # which machines received this file
gipper custody check ./secrets.env        # was this exact value ever placed on a machine
```

`report` has four sections:

- **Rotate:** credentials placed on machines, with how many machines and distinct values.
- **Review:** uploaded files that look sensitive.
- **Machines:** each rental with its machine ID, job, uploads and env names.
- **Gaps:** what the report can't vouch for. Read this section before relying on the others.

Add `--json` for the whole report as data. `--pod`, `--machine` and `--until` narrow it.

## What is recorded

**Each upload.** `Remote.put` reads the tar stream it is about to send, so exclude rules
apply exactly as they will on the wire. It stores a manifest (path, size, sha256, mode)
under `~/.local/share/gipper/custody` with owner-only permissions, then writes a
`custody.upload` receipt. Only then does anything leave. If the manifest can't be taken,
the upload doesn't happen. A `custody.uploaded` receipt records the outcome. An upload
with no outcome, or a failed one, may still have delivered bytes, so treat it as arrived.

**Each machine.** `custody.machine` records provider, machine ID, address, image, env
names, and hashes of the job file and command.

**Sensitive material.** Uploads are checked for file names like `.env`, `id_ed25519`
and `*.pem`, and content patterns like private keys and common API tokens. Findings keep
the path and rule, never the matched text. Files over 2 MB are hashed but not scanned,
and archives are not opened. Both appear under Gaps.

**Credentials.** Values are never recorded. A fingerprint is a keyed hash, with the key
kept beside the ledger, so `check` can recognise a value without the ledger being
usable to guess it. After you rotate, `check` on the new values should report that none
has been seen on a machine.

## Placing a credential on a machine

Don't upload credential files. Do this instead, which records the names:

```python
remote.put_secrets({"SERVICE_TOKEN": token}, "/root/job/service.env")
```

The file is mode 600, and the values travel over stdin rather than the command line. It
is receipted before sending, so a failed write still counts as exposed.

## Recording what went around gipper

gipper can't see a `scp` you ran by hand, a tunnel, or a machine created in a provider's
console. Declare them, and they join the report:

```
gipper custody declare --provider runpod --pod POD_ID \
  --secret SERVICE_TOKEN --from-env ./secrets.env --path /root/job/service.env --how scp
gipper custody declare --provider runpod --pod POD_ID --file ./bundle --path /root/job/bundle --how scp
gipper custody declare --provider runpod --pod POD_ID --tunnel "reverse tunnel to local port 8917"
```

Use `--at` for something that happened earlier, and `--confidence inferred` when you
worked it out rather than saw it. `report --check-providers` compares providers' bills
with the ledger and lists machines gipper has never heard of. This call is read-only.

## Refusing uploads

```toml
# ~/.config/gipper/config.toml
custody_on_sensitive = "refuse"                # default "warn": record it and say so
custody_secret_files = ["~/.config/app/.env"]  # these values must never be uploaded
```

```toml
# in a job
[custody]
on_sensitive = "refuse"
allow = ["tests/fixtures/*.pem"]               # reviewed false positives
```

Provider keys and values from `custody_secret_files` are always refused, even when a
path is allowed. Jobs are checked before renting, so a refusal doesn't cost a machine.

## Limits

- It records what passed through gipper or was declared. Commands run on a machine, and
  anything a job downloaded for itself, are not recorded. The command is kept as a hash.
- Outputs brought back from a machine have no manifest yet.
- Scanning catches common shapes and known values, not every secret. A clean scan is not
  proof that a file holds none.
- Providers may add credentials of their own to a machine, such as a machine-scoped API
  key. Check your provider's console for keys that outlived their machines.
- The ledger is append-only by convention and isn't tamper-proof. Treat it as your own
  record, not as evidence against an attacker who had access to this machine.
- A trail only exists from the release that adds it. Earlier rentals show under Gaps.
