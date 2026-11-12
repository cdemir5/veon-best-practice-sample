from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Request

from app.state import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]

# Girdi doğrulaması: Redis anahtarına giren her değer sınırlı karakter kümesinden.
SkuPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,32}$")]
