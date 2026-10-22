import operator
from typing import Any, Callable, Union


class BaseFilter:
    def __call__(self, obj: Any) -> bool:
        raise NotImplementedError

    def __and__(self, other):
        return LogicalFilter(self, other, op=operator.and_)

    def __or__(self, other):
        return LogicalFilter(self, other, op=operator.or_)

    def __invert__(self):
        return NotFilter(self)


class AlwaysTrueFilter(BaseFilter):
    def __call__(self, obj: Any) -> bool:
        return True


class HasAttrFilter(BaseFilter):
    def __init__(self, attr: str):
        self.attr = attr

    def __call__(self, obj: Any) -> bool:
        return hasattr(obj, self.attr)


class AttrEqualsValueFilter(BaseFilter):
    def __init__(self, attr: str, value: Any):
        self.attr = attr
        self.value = value

    def __call__(self, obj: Any) -> bool:
        return getattr(obj, self.attr, None) == self.value


class AttrFilter(BaseFilter):
    def __init__(self, path=()):
        self.path = path

    def __getattr__(self, attr):
        return AttrFilter(self.path + (attr,))

    def __getitem__(self, key):
        return AttrFilter(self.path + (("__getitem__", key),))

    def __call__(self, *args, **kwargs):
        return AttrFilter(self.path + (("__call__", args, kwargs),))

    def _resolve(self, obj: Any):
        for part in self.path:
            if isinstance(part, tuple):
                if part[0] == "__getitem__":
                    key = part[1]
                    try:
                        obj = obj[key]
                    except (KeyError, TypeError, IndexError):
                        return None
                elif part[0] == "__call__":
                    args, kwargs = part[1], part[2]
                    try:
                        obj = obj(*args, **kwargs)
                    except Exception:
                        return None
            else:
                try:
                    obj = getattr(obj, part)
                except AttributeError:
                    return None
        return obj

    def __eq__(self, value):
        return CompareFilter(self, value, operator.eq)

    def __ne__(self, value):
        return CompareFilter(self, value, operator.ne)

    def __lt__(self, value):
        return CompareFilter(self, value, operator.lt)

    def __le__(self, value):
        return CompareFilter(self, value, operator.le)

    def __gt__(self, value):
        return CompareFilter(self, value, operator.gt)

    def __ge__(self, value):
        return CompareFilter(self, value, operator.ge)

    def __contains__(self, item):
        return CompareFilter(self, item, lambda a, b: b in a)

    def __call__(self, obj: Any):
        return bool(self._resolve(obj))

    def __repr__(self):
        def part_str(p):
            if isinstance(p, tuple):
                if p[0] == "__getitem__":
                    return f"[{p[1]!r}]"
                elif p[0] == "__call__":
                    return f"()"
            return f".{p}"

        return f"Attr({''.join(part_str(p) for p in self.path)})"


class CompareFilter(BaseFilter):
    def __init__(self, left: AttrFilter, right: Any, op: Callable):
        self.left = left
        self.right = right
        self.op = op

    def __call__(self, obj: Any):
        return self.op(self.left._resolve(obj), self.right)

    def __repr__(self):
        return f"({self.left} {self.op.__name__} {self.right})"


class LogicalFilter(BaseFilter):
    def __init__(self, left: BaseFilter, right: BaseFilter, op: Callable):
        self.left = left
        self.right = right
        self.op = op

    def __call__(self, obj: Any):
        return self.op(self.left(obj), self.right(obj))

    def __repr__(self):
        return f"({self.left} {self.op.__name__} {self.right})"


class NotFilter(BaseFilter):
    def __init__(self, inner: BaseFilter):
        self.inner = inner

    def __call__(self, obj: Any):
        return not self.inner(obj)

    def __repr__(self):
        return f"~{self.inner}"


# Entry point
F = AttrFilter()
