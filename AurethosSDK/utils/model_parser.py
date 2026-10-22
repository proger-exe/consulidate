import json
import logging
from typing import Dict

from AurethosSDK.types.base_model import Generation, Model

logger = logging.getLogger(__name__)


def model_parser(path_to_file: str) -> Dict[str, Model]:
    """
    Parse a JSON file to initialize and return a mapping of model names to Model instances.

    Reads configuration from the given file path, enforces a single priority-zero model
    as the CORE, and applies generation settings if provided.

    :param path_to_file: Path to the JSON configuration file
    :type path_to_file: str
    :return: Dictionary mapping model names to Model objects
    :rtype: Dict[str, Model]
    :raises TypeError: If more than one model has Priority = 0
    """
    logger.debug("Opening configuration file: %s", path_to_file)
    models: Dict[str, Model] = {}
    priority_set = False
    default_generation = Generation()

    with open(path_to_file, "r+", encoding="utf-8") as file:
        data = json.load(file)
        logger.debug("Loaded JSON data: %s", data)

        for entry in data:
            name = entry.get("Name")
            priority = entry.get("Priority", 1)
            logger.debug("Processing model entry: name=%s, priority=%d", name, priority)

            if priority == 0:
                if priority_set:
                    logger.error("Multiple priority-zero models detected: %s", entry)
                    raise TypeError("Cannot init two models with `Priority` = 0")
                priority_set = True
                name = "CORE"
                logger.debug("Priority-zero model set as CORE")

            generation_setting = default_generation
            gen_info = entry.get("Generation")
            if gen_info:
                logger.debug(
                    "Found generation settings for model %s: %s", name, gen_info
                )
                gen_kwargs = {
                    "temperature": gen_info.get(
                        "temperature", generation_setting.temperature
                    ),
                    "top_p": gen_info.get("top_p", generation_setting.top_p),
                    "top_k": gen_info.get("top_k", generation_setting.top_k),
                    "max_output_tokens": gen_info.get(
                        "max_output_tokens", generation_setting.max_output_tokens
                    ),
                    "response_mime_type": gen_info.get(
                        "response_mime_type", generation_setting.response_mime_type
                    ),
                }
                generation_setting = Generation(**gen_kwargs)
                logger.debug(
                    "Initialized Generation for model %s: %s", name, generation_setting
                )

            model_kwargs = {
                "name": name,
                "system_instruction": entry.get("SystemInstruction"),
                "upload_from_file": entry.get("FromFile", False),
                "priority": priority,
                "use_model": entry.get("ModelUse", "openai-4.1"),
                "use_model_cheap": entry.get("ModelUseCheap"),
                "generation_setting": generation_setting,
            }
            logger.debug("Instantiating Model with args: %s", model_kwargs)
            models[name] = Model(**model_kwargs)
            logger.debug("Registered model: %s", models[name])

    logger.debug("Model parsing complete. Parsed models: %s", list(models.keys()))
    return models
