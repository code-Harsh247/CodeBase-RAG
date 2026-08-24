import os.path

from pkg import Animal
from pkg.services import make_dog


def main() -> None:
    make_dog()
    os.path.join("a", "b")


def unused(animal: Animal) -> None:
    animal.describe()
