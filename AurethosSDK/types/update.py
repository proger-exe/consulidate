from typing import Any, Dict

from pydantic import BaseModel


class Update(BaseModel):
    to_model: str
    from_model: str
    command: str
    parameters: Dict[str, Any]
