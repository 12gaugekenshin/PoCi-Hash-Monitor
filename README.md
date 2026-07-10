# PoCiSys Hash Monitor

Complete umbrelOS Community App Store repository.

Add this Community App Store URL in Umbrel:

```text
https://github.com/12gaugekenshin/PoCi-Hash-Monitor
```

Version 1.4.21 uses the public multi-architecture Python image. The container
downloads this repository's app source at startup and runs it from `/tmp`, so
Umbrel only persists `/data/config.json`. It does not require GitHub Actions,
pip installs, bind-mounted repo files, or a custom container package. Config
saves use a simple direct JSON write for maximum UmbrelOS compatibility, and
a tiny supervisor restarts the backend if it ever exits unexpectedly.

## License

PoCiSys Hash Monitor is released under the Apache License, Version 2.0.
See `LICENSE` and `NOTICE`.
