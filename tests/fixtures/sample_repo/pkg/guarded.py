from typing import TYPE_CHECKING

try:
    import json
except ImportError:
    json = None

if TYPE_CHECKING:
    from pkg.models import Dog


def describe(dog: Dog) -> None:
    dog.speak()
