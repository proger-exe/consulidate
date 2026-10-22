from typing import Any, Dict

from AurethosSDK.types.update import Update


def as_update(from_model: str, to_model: str, command: str, parameters: Dict[str, Any]):
    return Update(
        to_model=to_model, from_model=from_model, command=command, parameters=parameters
    )


def dict_as_update(
    update: dict,
):
    return Update(
        to_model=update["to_model"],
        from_model=update["from_model"],
        command=update["command"],
        parameters=update.get("parameters", {}),
    )
