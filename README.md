# PoCiSys Hash Monitor

Complete umbrelOS Community App Store repository.

Add this Community App Store URL in Umbrel:

```text
https://github.com/12gaugekenshin/PoCi-Hash-Monitor
```

Version 1.5.1 uses the public multi-architecture Python image. The container
downloads this repository's app source at startup and runs it from `/tmp`, so
Umbrel only persists `/data/config.json`. It does not require GitHub Actions,
pip installs, bind-mounted repo files, or a custom container package. Config
saves use a simple direct JSON write for maximum UmbrelOS compatibility, and
a tiny supervisor restarts the backend if it ever exits unexpectedly.

The latest release adds authenticated, read-only Hermes MCP support. Hermes can
query current miner health, Public Pool workers and hashrate, SHA-256 block
odds, and container-visible Umbrel host CPU, RAM, disk, load, and uptime
metrics. PoCiSys exposes no miner controls, shell execution, wallet data, or
configuration tools through MCP. Connection tokens are shown once and stored
only as SHA-256 digests.

Version 1.5.1 also fixes intermittent `502` errors on Umbrel systems running
Public Pool. PoCiSys now targets its unique Docker network alias instead of the
shared, ambiguous `server` hostname.

See [HERMES-SETUP.md](HERMES-SETUP.md) for the connection steps.

Built by [12GaugeKenshin](https://github.com/12gaugekenshin).

**PoCiSys is building an open, verifiable infrastructure stack for AI auditing
and cryptocurrency mining.** Its AI auditing system monitors model and agent
behavior through timing, performance, configuration, and operational signals,
helping detect drift, tampering, unauthorized changes, and other anomalies
without exposing private prompts, training data, or proprietary models.

Alongside it, PoCi Hash Monitor provides lightweight tools for managing mining
hardware, monitoring pools and nodes, tracking performance, and supporting
self-hosted BTC, BCH, and KAS infrastructure. Together, these systems create a
transparent foundation for proving compute integrity across both AI and
decentralized networks.

- Website: https://pocisys.io/
- X: https://x.com/12gaugekenshin

## License

PoCiSys Hash Monitor is released under the Apache License, Version 2.0.
See `LICENSE` and `NOTICE`.
