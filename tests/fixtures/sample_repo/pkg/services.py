from pkg.models import Dog


class Kennel:
    def add(self, dog: Dog) -> None:
        dog.speak()


def find_dog() -> Dog:
    raise NotImplementedError


def make_dog() -> Dog:
    d = Dog()
    d.speak()
    return d
