import logging
from typing import Callable, List, Optional, Union

from AurethosSDK.client.router import ABSRouter, Router
from AurethosSDK.models.base import BaseModel
from AurethosSDK.types.base_model import Model
from AurethosSDK.types.update import Update
from AurethosSDK.utils.model_parser import model_parser

# Type alias for trampoline return values
ThunkOrResult = Union[Update, None, Callable[[], "ThunkOrResult"]]


def trampoline(fn: Callable[..., ThunkOrResult]) -> Callable[..., Optional[Update]]:
    """
    Trampoline decorator to convert recursion into iteration.

    The decorated function should return one of:
      - an Update instance: final result of processing
      - None: indicates no further processing
      - a zero-argument callable (thunk) that returns another ThunkOrResult

    The trampoline runner will repeatedly invoke the returned thunk
    until an Update or None is obtained, thereby preventing deep recursion
    and potential stack overflows.

    :param fn: Function implementing recursive logic via returning thunks
    :type fn: Callable[..., ThunkOrResult]
    :return: Wrapped function that runs iteratively and returns Optional[Update]
    :rtype: Callable[..., Optional[Update]]
    """

    def runner(*args, **kwargs) -> Optional[Update]:
        result = fn(*args, **kwargs)
        while callable(result):  # If it's a thunk, invoke it
            result = result()
        return result  # Either Update or None

    return runner


class Dispatcher:
    def __init__(self, path_to_models: str):
        self.path_to_models = path_to_models
        self.routers: dict[str, Union[Router, ABSRouter]] = {}
        self.models: dict[str, Model] = {}
        self.logger = logging.getLogger("Aurethos.dp")

    def _upload_models(self) -> None:
        models = model_parser(self.path_to_models)
        self.models.update(models)
        self.logger.debug("Uploaded models: %s", list(models.keys()))

    def init_routers(self, **kwargs) -> None:
        self._upload_models()
        for model_name, model_conf in self.models.items():
            base_model = BaseModel(model_conf)
            router = Router(base_model, **kwargs)
            if abs_router := self.routers.get(model_name):
                router.sync(abs_router.handlers)

            self.routers[model_name] = router
            self.logger.debug("Initialized router: %s", base_model.settings.name)

    @trampoline
    def check_update(self, update: Update) -> ThunkOrResult:
        """
        Dispatch an Update through the router chain using trampolined calls.

        :param update: Update object with 'to_model' indicating target router
        :type update: Update
        :return: Either an Update, None, or a thunk for next dispatch
        """
        target = update.to_model
        router = self.routers.get(target)
        if not router or isinstance(router, ABSRouter):
            self.logger.debug("No router found for: %s", target)
            return update

        self.logger.debug("Dispatching update to router: %s", target)
        result = router.check_update(update)
        if not result:
            self.logger.debug("Router %s returned no result", target)
            return None

        return lambda: self.check_update(result)

    def include_routers(self, routers: List[ABSRouter]):
        for router in routers:
            self.routers.update({router.model_name: router})

    # TODO: bind_system_prompt_args (for users)
    # TODO: system_prompt_formatting. (for system)
    # TODO: call this `system_prompt_formatting` before start_polling

    def start_polling(
        self, custom_update: Optional[Update] = None, no_update: bool = False, **kwargs
    ) -> None:
        """
        Initialize routers and begin processing an initial Update or event loop.

        :param custom_update: Optional initial Update to inject
        :param no_update: If True, skip immediate dispatch
        :param kwargs: Parameters for initial START command
        """
        self.init_routers(**kwargs)
        if not custom_update:
            custom_update = Update(
                to_model="CORE", from_model="CORE", command="START", parameters=kwargs
            )

        self.logger.info("Starting dispatcher polling")

        if not no_update:
            self.check_update(custom_update)
