import inspect
import importlib
import backtrader as bt


def effective_doc(cls):
    """
    Полный docstring класса.
    Если у самого класса docstring нет — берём первый найденный у базовых классов (по MRO).
    Если хочешь ТОЛЬКО docstring именно этого класса — замени тело на:
        return inspect.getdoc(cls)
    """
    for c in cls.mro():
        if c is object:
            continue
        if c.__doc__:
            return inspect.cleandoc(c.__doc__)
    return None


def print_class(name, cls):
    doc = effective_doc(cls)
    if not doc:
        print(name)
        return

    print(name)
    for line in doc.splitlines():
        print(f"    {line}")


def iter_classes(module, base=None):
    for name, obj in vars(module).items():
        if not inspect.isclass(obj):
            continue
        # не печатаем "притащенные" из других модулей классы
        if not obj.__module__.startswith(module.__name__):
            continue
        if base is not None:
            try:
                if not issubclass(obj, base):
                    continue
            except TypeError:
                continue
        yield name, obj


def pick_feed_base():
    for modname in ("backtrader.feed", "backtrader.feeds"):
        try:
            mod = importlib.import_module(modname)
            base = getattr(mod, "DataBase", None)
            if inspect.isclass(base):
                return base
        except Exception:
            pass
    return None


def print_core():
    print("=== Core (bt.<...>) ===")
    for n in [
        "Cerebro", "Strategy", "Indicator", "Analyzer", "Observer", "Sizer",
        "Order", "Trade", "CommInfoBase", "BrokerBase",
    ]:
        cls = getattr(bt, n, None)
        if inspect.isclass(cls):
            print_class(f"bt.{n}", cls)
        else:
            print(f"bt.{n}")


def print_categories():
    print("\n=== Built-ins by category ===")
    feed_base = pick_feed_base()

    categories = [
        ("feeds",      bt.feeds,      feed_base),
        ("indicators", bt.indicators, bt.Indicator),
        ("analyzers",  bt.analyzers,  bt.Analyzer),
        ("observers",  bt.observers,  bt.Observer),
        ("sizers",     bt.sizers,     bt.Sizer),
    ]

    for title, mod, base in categories:
        print(f"\n-- {title} ({mod.__name__}) --")
        items = sorted(iter_classes(mod, base=base), key=lambda x: x[0].lower())
        for name, cls in items:
            print_class(f"{mod.__name__}.{name}", cls)


if __name__ == "__main__":
    print_core()
    print_categories()
