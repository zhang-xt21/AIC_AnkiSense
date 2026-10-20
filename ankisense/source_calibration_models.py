from pydantic import Field
from typing import Annotated
from .schemas import StrictModel


class PageTranscription(StrictModel):
    text: str = Field(min_length=1, max_length=16000)
    uncertainties: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=32)


class CalibrationRequest(StrictModel):
    revision: int = Field(ge=1, strict=True)
    force: bool = Field(default=False, strict=True)
