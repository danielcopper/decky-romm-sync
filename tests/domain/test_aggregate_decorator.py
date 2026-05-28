"""Unit tests for the ``@cosmic_aggregate`` decorator."""

from __future__ import annotations

from dataclasses import field

import pytest

from domain._aggregate import cosmic_aggregate


class TestDecoratorMakesDataclass:
    def test_class_gains_dataclass_init(self):
        @cosmic_aggregate
        class Sample:
            name: str
            count: int

        instance = Sample(name="alpha", count=3)
        assert instance.name == "alpha"
        assert instance.count == 3

    def test_class_gains_dataclass_repr(self):
        @cosmic_aggregate
        class Sample:
            name: str

        # dataclass-generated repr uses the class qualname; nested test
        # classes carry the enclosing-scope prefix.
        rendered = repr(Sample(name="alpha"))
        assert rendered.endswith("Sample(name='alpha')")

    def test_class_gains_dataclass_eq(self):
        @cosmic_aggregate
        class Sample:
            name: str
            count: int

        assert Sample(name="a", count=1) == Sample(name="a", count=1)
        assert Sample(name="a", count=1) != Sample(name="a", count=2)


class TestDecoratorAppliesSlots:
    def test_class_has_slots_attribute(self):
        @cosmic_aggregate
        class Sample:
            name: str
            count: int

        assert hasattr(Sample, "__slots__")

    def test_instance_rejects_unknown_attribute(self):
        @cosmic_aggregate
        class Sample:
            name: str

        instance = Sample(name="alpha")
        with pytest.raises(AttributeError):
            instance.undeclared = "boom"  # pyright: ignore[reportAttributeAccessIssue]

    def test_instance_has_no_instance_dict(self):
        """`__slots__` implies no per-instance __dict__ — proves slots took effect."""

        @cosmic_aggregate
        class Sample:
            name: str

        instance = Sample(name="alpha")
        assert not hasattr(instance, "__dict__")


class TestDecoratorEdgeCases:
    def test_supports_field_defaults(self):
        @cosmic_aggregate
        class Sample:
            name: str
            count: int = 0

        assert Sample(name="alpha").count == 0
        assert Sample(name="alpha", count=5).count == 5

    def test_supports_default_factory(self):
        @cosmic_aggregate
        class Sample:
            tags: list[str] = field(default_factory=list)

        a = Sample()
        b = Sample()
        a.tags.append("x")
        # Each instance gets its own list — default_factory wired correctly.
        assert a.tags == ["x"]
        assert b.tags == []

    def test_methods_coexist_with_decorator(self):
        @cosmic_aggregate
        class Sample:
            count: int

            def doubled(self) -> int:
                return self.count * 2

        assert Sample(count=3).doubled() == 6

    def test_mutation_via_method_works(self):
        """Slots allow assignment to declared fields — only undeclared names are rejected."""

        @cosmic_aggregate
        class Sample:
            count: int

            def increment(self) -> None:
                self.count += 1

        instance = Sample(count=0)
        instance.increment()
        assert instance.count == 1

    def test_preserves_class_name(self):
        @cosmic_aggregate
        class Sample:
            name: str

        assert Sample.__name__ == "Sample"

    def test_preserves_class_module(self):
        @cosmic_aggregate
        class Sample:
            name: str

        assert Sample.__module__ == __name__
