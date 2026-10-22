from typing import Callable, List

from AurethosSDK.filters.filter import BaseFilter
from AurethosSDK.types.update import Update


class Handler:
    def __init__(self, function: Callable, filter: BaseFilter):
        self.filters = filter
        self.function = function

    def __call__(self, update: Update, *args, **kwargs):
        if self.filters(update):
            return self.function(*args, **kwargs, update=update)

    def prevent_check(self, update: Update):
        return self.filters(update)
