from .utils import helper


class Animal:
    def speak(self) -> str:
        return "..."

    def describe(self) -> str:
        return self.speak()


class Dog(Animal):
    def speak(self) -> str:
        helper()
        return "woof"

    def fetch(self, times: int) -> None:
        self.speak()
