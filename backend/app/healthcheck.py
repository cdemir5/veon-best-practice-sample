"""Worker liveness kontrolü: `python -m app.healthcheck`

Kuyruk worker'larının HTTP sunucusu yoktur. Ana döngü her turda bir dosyaya
zaman damgası yazar; dosya `WORKER_HEARTBEAT_MAX_AGE` saniyeden eskiyse döngü
takılmış (deadlock/sonsuz bekleme) demektir → exit 1 → kubelet container'ı yeniden başlatır.

Not: Redis/Mongo kesintisinde döngü dönmeye devam eder (bekle-tekrar dene),
dolayısıyla bağımlılık kesintisi gereksiz restart'a yol açmaz.
"""
from __future__ import annotations

import os
import sys
import time


def main() -> int:
    path = os.getenv("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat")
    max_age = float(os.getenv("WORKER_HEARTBEAT_MAX_AGE", "60"))
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return 1
    return 0 if age < max_age else 1


if __name__ == "__main__":
    sys.exit(main())
