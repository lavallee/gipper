# Roadmap

The current release runs rented NVIDIA GPU jobs with cost estimates, checkpoints,
verified teardown, and ledger-based placement memory.

Potential next steps, without delivery commitments:

- Qualify Shadeform with real smoke and UDP tests before calling it supported.
- Coordinate owned GPUs and rented capacity through requests and leases.
- Add priorities and pre-emption with explicit resource ownership.

- Extend the custody trail to outputs brought back from a machine, and look inside
  uploaded archives.

These features are not implemented by the current CLI.
