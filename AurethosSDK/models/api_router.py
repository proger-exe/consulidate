import inspect
import logging
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Union

from typing_extensions import deprecated

from AurethosSDK.models import codex_gateway
from AurethosSDK.models.base import BaseModel
from AurethosSDK.utils.history import HistoryStorage

# Only codex_gateway is active. To re-enable others, restore imports:
# `from AurethosSDK.models import chatgot, forgeml, llm7, pandarobot, pollinations`
# and add their client classes back to APIS_OBJECTS.
APIS_OBJECTS = {
    "codex_gateway": codex_gateway.CodexGatewayClient,
}
#   INSTRUCTIONS FOR ADDING a new model's class API:
#   You need the `checkup`(bool) parameter in the __init__ of class,
#   `check_status` method to check the status(should return tuple[bool, dict[str, Any] | None])
#   And the names of the methods for communicating with the models should be the same,
#   it was `chat_completion`, so i left it like that


class APIRouter:
    """
    Router for APIs classes, initialize, check status and select working API.
    """

    def __init__(self, model: BaseModel, **kwargs):
        """
        Router initialize method.

        Args:
            model (BaseModel): Model object, configuration and settings.
            **kwargs: Keyword params for model object.
        """

        # system API Router settings and config
        self.logger = logging.getLogger(self.__class__.__name__)
        self.status_check_method_name = "check_status"
        self.send_request_method_name = "chat_completion"

        # model object and their settings/config
        self.model = model
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        # dict, used to pass class __init__ method
        self.mandatory_args = {"model": self.model, "checkup": True}

        # API Router storages
        self.initialized_classes: Dict[str, type] = {}
        self.aviable_apis: List[str] = None
        self.model_history = HistoryStorage()

    @deprecated(
        "Not used now anywhere in APIRouter class, don't try to use it, can broke something ;)"
    )
    def change_init_model(
        self, new_model: BaseModel, **kwargs
    ) -> None:  # not used now, maybe will be removed
        """
        Change BaseModel object and set up new `kwargs` in it.

        Args:
            new_model (BaseModel): Model object, configuration and settings.
            **kwargs: New keyword params for model object.
        """
        self.model = new_model
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        self.mandatory_args["model"] = self.model

        self.logger.debug("Object model changed to new BaseModel object=%s", new_model)

    # here logs not need i think, if needed - would add
    def _validate_init_args(self, cls: type) -> Callable:
        """
        Decorator factory that validates and sanitizes initialization arguments for a class.

        This decorator performs three key validations before class initialization:
        1. Verifies all required arguments (non-optional parameters) are provided
        2. Includes optional parameters with their default values when not provided
        3. Preserves additional kwargs if the class accepts variable keyword arguments

        Args:
            cls (type): The class whose __init__ method should be validated. Must have an
                inspectable __init__ signature.

        Returns:
            A decorator function that wraps the class's `__init__` method with validation.
            The decorated `__init__` will:
            - Raise ValueError if required arguments are missing
            - Automatically inject default values for optional parameters
            - Preserve extra kwargs if the class accepts **kwargs

        Example:
            >>> api_router = APIRouter()
            >>> MyClass.__init__ = api_router._validate_init_args(MyClass)(MyClass.__init__)
            >>> instance = MyClass(req_arg=1)  # Validates during initialization

        Notes:
            - The decorator preserves the original function's metadata using @wraps
            - Validation is performed against the class's __init__ signature
            - Does not modify the class itself, only its initialization behavior
        """

        def decorator(init_func: Callable) -> Callable:
            @wraps(init_func)
            def wrapper(self, *args, **kwargs):
                # Get expected parameters from API class's __init__
                sig = inspect.signature(cls.__init__)
                params = sig.parameters

                # 1. Check required parameters (no default)
                required_params = [
                    name
                    for name, param in params.items()
                    if (
                        name not in ["self", "kwargs"]
                        and param.default == inspect.Parameter.empty
                    )
                ]
                missing = [p for p in required_params if p not in kwargs]
                if missing:
                    raise ValueError(f"Missing required arguments: {missing}")

                # 2. Prepare allowed kwargs (include all declared parameters)
                allowed_kwargs = {}
                for name, param in params.items():
                    if name in ["self", "kwargs"]:
                        continue
                    if name in kwargs:
                        allowed_kwargs[name] = kwargs[name]
                    elif param.default != inspect.Parameter.empty:
                        # Include optional parameters with their defaults
                        allowed_kwargs[name] = param.default

                # 3. Preserve additional kwargs if **kwargs exists in signature
                if "kwargs" in params:
                    extra_kwargs = {k: v for k, v in kwargs.items() if k not in params}
                    allowed_kwargs.update(extra_kwargs)

                return init_func(self, *args, **allowed_kwargs)

            return wrapper

        return decorator

    def _init_api_class(self, cls: type, **kwargs) -> type:
        """
        Initialize API class with argument validation and return initialized class object.

        Args:
            cls (type): Class object.
            **kwargs: keyword params for model object, would be transfered to class object.

        Returns:
            type: Instance of initialized class object.
        """
        self.logger.debug(
            "Validating recived kwargs and initializing class=%s", cls.__name__
        )
        try:
            # validate args and init passed class object
            cls.__init__ = self._validate_init_args(cls)(cls.__init__)
            instance: type = cls(**kwargs)
            return instance
        except ValueError as e:
            self.logger.exception(
                "Class initialization or arguments validatinon failed, class=%s, error: %r",
                cls.__name__,
                str(e),
            )
            raise

    def _method_exists(self, cls: type, method_name: str) -> bool:
        """Check if class has the specified method.

        Args:
            cls (type): Class object.
            method_name (str): Method name that need to be finded(checked if it's exists).

        Returns:
            bool: `True` if method exists, otherwise `False`.
        """
        self.logger.debug(
            "Checking if class=%s has method with name=%r",
            cls.__class__.__name__,
            method_name,
        )
        return callable(getattr(cls, method_name, None))

    def _call_class_method(
        self, instance: type, method_name: str, *args, **kwargs
    ) -> Union[object, None]:
        """
        Dynamically calls a method by name on an initialized class instance.

        Args:
            instance (type): Initialized class instance
            method_name (str): Name of the method to call
            *args: Positional arguments to pass to the method
            **kwargs: Keyword arguments to pass to the method

        Returns:
            The return value of the called method
        """
        # check if initialized class has method `method_name`
        if not self._method_exists(instance, method_name):
            self.logger.critical(
                "Method %r not founded in class=%s",
                method_name,
                instance.__class__.__name__,
            )
            return None

        self.logger.debug(
            "Initializing API class method=%r from class=%s with args=%s, kwargs=%s",
            method_name,
            instance.__class__.__name__,
            args,
            kwargs,
        )
        method: object = getattr(instance, method_name, None)

        return method(*args, **kwargs)

    def _get_attribute(
        self, instance: type, attr_name: str, default: Any = None
    ) -> Any:
        """
        Safely gets an attribute from an object by name.

        Args:
            instance (type): Initialized object
            attr_name (str): Name of attribute to check
            default (Any): Value to return if attribute doesn't exist

        Returns:
            The attribute value if exists, otherwise default
        """
        return getattr(instance, attr_name, default)

    def check_single_api(
        self, name: str, **kwargs
    ) -> bool:  # thinking about type of return, jsut test variant
        """
        Check single API alive status

        Args:
            name (str): API class name.
            **kwargs: Keyword params for model object, would passed to class object.

        Returns:
            bool: `True` if API is alive, otherwise `False`.
        """
        # check if passed `name` is in `APIS_OBJECTS` and get it
        class_object: type = APIS_OBJECTS.get(name, None)
        if class_object is None:
            self.logger.critical("Object of API class by name=%r not founded", name)
            return False  # or use raise

        # get and update mandatory args for class init
        request_args = self.mandatory_args.copy()
        request_args.update(kwargs)

        self.logger.debug(
            "Recieved class initialization, recived name=%r, finded class=%s, transmitting keyword arguments=%s, settings=%r",
            name,
            class_object.__name__,
            request_args,
            {
                k: "Too Long" if k == "system_instruction" else v
                for k, v in self.model.settings.model_dump().items()
            },
        )
        # initialize class with args and get its object, update `initialized_classes` list
        initialized_class = self._init_api_class(class_object, **request_args)
        self.initialized_classes.update({name: initialized_class})

        # call method to check status of API, get result
        api_alive: bool
        status_report: Optional[Dict[str, Any]]
        call_result = self._call_class_method(
            initialized_class, self.status_check_method_name
        )
        if not call_result:
            return False  # or use raise

        api_alive, status_report = call_result
        self.logger.info(
            "Method=%r from class=%s returned: api_alive=%s, status_report=%r",
            self.status_check_method_name,
            class_object.__name__,
            api_alive,
            status_report,
        )

        return api_alive

    def check_all_apis(
        self, **kwargs
    ) -> Dict[str, bool]:  # here also thinking about type of return, just test variant
        """
        Check all APIs statuses.
        After checking updates `self.aviable_apis` var.

        Args:
            **kwargs: Keyword params for model object, would passed to all classes objects.

        Returns:
            out (dict[str, bool]): Dict object with all alive APIs.
        """
        self.logger.debug("Starting to check status of all APIs=%s", APIS_OBJECTS)

        # just go through the `APIS_OBJECTS` list and check each API
        checks_result: Dict[str, bool] = (
            {}
        )  # left it for the future, i know that it's could have been written more compactly
        for key in APIS_OBJECTS.keys():
            checks_result.update({key: self.check_single_api(key, **kwargs)})

        # sort on only aviable APIs and update `aviable_apis` list
        aviable_apis = {k: v for k, v in checks_result.items() if v is True}
        self.logger.debug(
            "All APIs status check finished, checks_result=%s, aviable_apis=%s",
            checks_result,
            aviable_apis,
        )

        self.aviable_apis = list(aviable_apis.keys())

        return aviable_apis

    def send_api_request(
        self,
        message: str,
        *args,
        expects: Optional[List[str]] = None,
        retries: int = 2,
        cheap: bool = False,
        _backoff_attempt: int = 0,
        **kwargs,
    ) -> Union[Union[Optional[Dict], str], None]:
        """
        Send request to model API throw initialized class, clear history in class after each try to send request.

        Args:
            message (str): Message for the model.
            *args: Positional arguments for model object, would passed to all classes objects.
            expects (Optional[List[str]]): Whitelist of allowed values for the `command`
                field in the model's JSON response. When set, the request message is prefixed
                with an ALLOWED_COMMANDS hint, and if the model still replies with a command
                outside this list, the router transparently retries with a corrective prompt
                up to `retries` times. Non-dict responses (str/None) skip validation.
            retries (int): Number of corrective retries left when the model returns a
                command that is not in `expects`. Ignored when `expects` is None.
            **kwargs: Keyword params for model object, would passed to all classes objects.

        Returns:
            out (Dict | str | None): `None` or `str` if there is no aviable APIs or some error occured,
            otherwise `Dict` with API model response.
        """
        if expects:
            message = f"[ALLOWED_COMMANDS: {expects}]\n{message}"

        provider_kwargs: Dict[str, Any] = {}
        if cheap and getattr(self.model.settings, "use_model_cheap", None):
            provider_kwargs["model"] = self.model.settings.use_model_cheap
            self.logger.debug(
                "cheap=True → routing via cheap model=%s",
                provider_kwargs["model"],
            )

        # start check if its first API Router sending request, just for logs, can be deprecated if needed
        if not self.aviable_apis:
            self.logger.debug("Aviable APIs list is None, starting to check...")
            self.check_all_apis(**kwargs)

        # start check if there is no aviable APIs
        elif len(self.aviable_apis) == 0:
            self.logger.debug(
                "Currently there is no aviable APIs, perform a recheck..."
            )
            self.check_all_apis(**kwargs)

        # if after rechecks there is no aviable APIs, return error
        if len(self.aviable_apis) == 0:
            self.logger.critical("After APIs check, there is no aviable APIs!")
            return "No aviable APIs"  # here also thinking about type of return

        # get only aviable initialized classes
        aviable_initialized_classes = {
            key: self.initialized_classes[key]
            for key in self.aviable_apis
            if key in self.initialized_classes
        }

        # pass on each class object and try to send request to model's API endpoint
        req_result: Union[Union[Optional[Dict], str], None] = None
        for class_name, initialized_class in aviable_initialized_classes.items():
            self.logger.info(
                "Sending api request to class=%s", initialized_class.__class__.__name__
            )
            # get from initialized class history object
            history_object = self._get_attribute(initialized_class, "history")

            # if `model_history` list have some history entries, import them to history of initialized class
            # otherwise add system entry
            if len(self.model_history.history) != 0:
                self._call_class_method(
                    history_object, "import_entires", self.model_history.history
                )
            else:
                # cache_control marker is a forward-compat hint for Anthropic-format
                # proxies (safely ignored by OpenAI-compat providers, which cache
                # long identical prefixes >=1024 tokens automatically anyway).
                self._call_class_method(
                    history_object,
                    "add_entry",
                    "system",
                    self.model.settings.system_instruction,
                    cache_control={"type": "ephemeral"},
                )

            # try to send request, if error or result is None, continue
            try:
                req_result = self._call_class_method(
                    initialized_class,
                    self.send_request_method_name,
                    message,
                    *args,
                    **provider_kwargs,
                )
                if req_result is None:
                    raise
            except codex_gateway.CodexGatewaySessionLostError:
                # gateway lost its remote session; scrub local history and
                # let the fallback loop pick another provider without
                # blacklisting the gateway itself (it may recover).
                if hasattr(history_object, "clear_history"):
                    self._call_class_method(history_object, "clear_history")
                self.logger.warning(
                    "Codex gateway session lost, falling through to next provider"
                )
                continue
            except:
                # if `model_history` list have some history entries, clear history in initialized class
                if len(self.model_history.history) != 0:
                    self._call_class_method(history_object, "clear_history")

                # remove initialized class object form `aviable_apis` list
                if class_name in self.aviable_apis:
                    self.aviable_apis.remove(class_name)

                self.logger.debug(
                    "Method=%r returned None or some error ocurred, class=%s removed from aviable APIs.",
                    self.send_request_method_name,
                    initialized_class.__class__.__name__,
                )
                continue

            # import all entries and clear hsitory from initialized class to `model_history` object
            self.model_history.import_entires(
                self._call_class_method(history_object, "get_all_entries", True)
            )
            break

        if req_result is None and len(self.aviable_apis) == 0:
            # All providers empty this pass. Sleep with exponential backoff
            # before retrying so we don't hammer a rate-limited backend from
            # multiple parallel workers. Cap at ~60s, give up after 6 tries.
            if _backoff_attempt >= 6:
                self.logger.critical(
                    "All providers still down after %d backoff attempts, giving up",
                    _backoff_attempt,
                )
                # Fatal path: snapshot memory to disk so the next run resumes
                # from the last completed file instead of restarting the whole
                # pipeline. Then unwind cleanly via CheckpointExitError which
                # main.py catches and exits(1).
                try:
                    from aurethos_coder.utils.checkpoint import (
                        CheckpointExitError,
                        save as _ckpt_save,
                    )
                    from aurethos_coder.utils.memory import memory as _mem

                    _ckpt_save(
                        _mem.snapshot(),
                        home_dir=_mem.get("home_path") or "./task/",
                        reason=f"gateway_exhausted_after_{_backoff_attempt}_backoffs",
                    )
                    raise CheckpointExitError(
                        f"gateway unreachable after {_backoff_attempt} backoff attempts; "
                        "state saved, rerun to resume"
                    )
                except ImportError:
                    # Checkpoint infra not available — old behaviour.
                    return None
            import time as _time
            wait_s = min(60, 2 ** _backoff_attempt)
            self.logger.warning(
                "All providers failed on attempt %d; sleeping %ds then retrying",
                _backoff_attempt + 1, wait_s,
            )
            _time.sleep(wait_s)
            return self.send_api_request(
                message,
                *args,
                expects=expects,
                retries=retries,
                cheap=cheap,
                _backoff_attempt=_backoff_attempt + 1,
                **kwargs,
            )

        # command whitelist enforcement: model must reply with `command` in `expects`.
        if expects and isinstance(req_result, dict):
            cmd = req_result.get("command")
            if cmd not in expects:
                self.logger.warning(
                    "Model replied with command=%r not in expects=%s (retries_left=%d)",
                    cmd,
                    expects,
                    retries,
                )
                if retries > 0:
                    # drop the bad turn from history so it doesn't poison future context
                    self.model_history.delete_last_by_count(2)
                    correction = (
                        f"Your previous reply used command={cmd!r} which is not "
                        f"allowed in this state. Reply again with a JSON containing "
                        f"exactly one of these commands: {expects}. No other commands."
                    )
                    return self.send_api_request(
                        correction,
                        *args,
                        expects=expects,
                        retries=retries - 1,
                        cheap=cheap,
                        _backoff_attempt=_backoff_attempt,
                        **kwargs,
                    )
                self.logger.critical(
                    "Model exhausted retries with invalid command=%r, expected one of %s. Returning as-is.",
                    cmd,
                    expects,
                )

        return req_result
