import inspect
import logging
from functools import wraps
from typing import Any, Callable, Dict, List, Type

from AurethosSDK.filters.filter import BaseFilter
from AurethosSDK.models.base import BaseModel
from AurethosSDK.types.handler import Handler
from AurethosSDK.types.update import Update
from AurethosSDK.utils.return_as import as_update, dict_as_update

# Configure module-level logger
logger = logging.getLogger(__name__)


class Router:
    """
    Router dispatches incoming updates to registered handlers.

    It holds a reference to a BaseModel instance and a list of
    Handler objects, each combining a callable and a filter.
    """

    def __init__(self, model: Type[BaseModel] | BaseModel, **custom_args) -> None:
        """
        Initialize the Router with a BaseModel instance.

        :param model: Instance of BaseModel for handler context
        :type model: BaseModel
        """
        self.model: Type[BaseModel] | BaseModel = model
        self.handlers: List[Handler] = []
        self.custom_args = custom_args
        logger.debug("Router initialized with model: %s", model)

    def __str__(self) -> str:
        """
        Return a string representation of the Router.

        :return: String listing model and handler count
        :rtype: str
        """
        return f"<Router model={self.model!r}, " f"handlers={len(self.handlers)}>"

    def set_custom_model(self, model: Type[BaseModel] | BaseModel):
        self.model = model

    def handler(self, filters: BaseFilter) -> Callable:
        """
        Decorator to register a new handler with associated filter.

        :param filters: Instance of BaseFilter to apply
        :type filters: BaseFilter
        :return: Decorator that wraps the handler function
        :rtype: Callable
        """
        logger.debug("Registering new handler with filter: %s", filters)

        def register_a_handler(func: Callable) -> Callable:
            """Internal decorator to wrap and register the handler."""
            logger.debug(
                "Handler appended: %s with filter %s",
                func.__name__,
                filters,
            )

            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                logger.debug(
                    "Handler %s invoked with args=%s, kwargs=%s",
                    func.__name__,
                    args,
                    kwargs,
                )

                type_map = {
                    Update: kwargs.get("update"),
                    BaseModel: kwargs.get("model"),
                    Router: kwargs.get("router"),
                }
                new_args = list(args)
                arg_names = func.__code__.co_varnames

                for i, val in enumerate(args):
                    if i < len(arg_names):
                        name = arg_names[i]
                        expected = func.__annotations__.get(name)
                        if expected in type_map:
                            new_args[i] = type_map[expected]
                            logger.debug("Converted arg '%s' to %s", name, new_args[i])

                sig = inspect.signature(func)
                valid_param_names = set(sig.parameters)

                new_kwargs: Dict[str, Any] = {}
                for key, val in kwargs.items():
                    if key not in valid_param_names:
                        logger.debug("Skipping unexpected kwarg: %s", key)
                        continue

                    expected = func.__annotations__.get(key)
                    if expected in type_map:
                        new_kwargs[key] = type_map[expected]
                        logger.debug("Converted kwarg '%s' to %s", key, new_kwargs[key])
                    else:
                        new_kwargs[key] = val

                result = func(*new_args, **new_kwargs)
                logger.debug("Handler %s returned %r", func.__name__, result)

                if isinstance(result, dict):
                    result = dict_as_update(result)
                    logger.debug("Converted dict result to Update: %s", result)
                    return result
                elif not isinstance(result, Update):
                    logger.debug(
                        "Handler %s returned None or unsupported, exiting",
                        func.__name__,
                    )
                    return None

                return result

            return wrapper

        return register_a_handler

    def check_update(self, update: Update, *args: Any, **kwargs: Any) -> Any:
        """
        Iterate through registered handlers and process the update.

        :param update: Incoming update to check
        :type update: Update
        :return: Result of handler or None
        :rtype: Any
        """

        logger.debug("Checking update: %s", update)
        for handler in self.handlers:

            logger.debug(
                "Evaluating handler %s with filter %s",
                handler.function.__name__,
                handler.filters,
            )

            if handler.prevent_check(update):
                logger.debug(
                    "Filter matched, calling handler: %s", handler.function.__name__
                )
                kwargs.update(model=self.model, router=self, update=update)
                kwargs.update(self.custom_args)
                kwargs.update(update.parameters)  # it's be useful

                result = handler(*args, **kwargs)

                logger.debug(
                    "Handler %s produced result: %s",
                    handler.function.__name__,
                    result,
                )

                return result
        logger.debug("No handler matched for update: %s", update)
        return None

    def sync(self, handlers: List[Handler]):
        logger.debug(f"Synchronizing filter with {handlers}")
        self.handlers.clear()

        for handler in handlers:
            orig = getattr(handler.function, "__wrapped__", handler.function)
            wrapped = self.handler(handler.filters)(orig)
            self.handlers.append(Handler(wrapped, handler.filters))


class ABSRouter:
    def __init__(self, model_name: str):
        self.model_name: str = model_name
        self.handlers: List[Handler] = []

    def handler(self, filters: BaseFilter) -> Callable:
        """
        Decorator to register a new handler with associated filter.

        :param filters: Instance of BaseFilter to apply
        :type filters: BaseFilter
        :return: Decorator that wraps the handler function
        :rtype: Callable
        """

        def register_a_handler(func: Callable) -> Callable:
            """Internal decorator to wrap and register the handler."""
            self.handlers.append(Handler(func, filters))

            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            return wrapper

        return register_a_handler
