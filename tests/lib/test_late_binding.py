"""Tests for ``lib.late_binding`` — typed two-phase forward reference."""

from __future__ import annotations

import pytest

from lib.late_binding import LateBinding


def test_get_before_set_raises():
    """Accessing an unset binding must raise RuntimeError tagged with the name."""
    binding: LateBinding[dict] = LateBinding("bios_files_index")

    with pytest.raises(RuntimeError, match="bios_files_index"):
        binding.get()


def test_get_invokes_bound_reader():
    """After set(), get() returns whatever the reader returns."""
    binding: LateBinding[dict] = LateBinding("pending_sync")
    binding.set(lambda: {"42": {"name": "Game"}})

    assert binding.get() == {"42": {"name": "Game"}}


def test_get_reflects_producer_rebinding():
    """get() observes producer-side attribute rebinds, not a snapshot.

    Regression for the captured-value design: ``LateBinding`` must hold a
    *reader function*, so each ``get()`` resolves the producer's current
    attribute rather than a reference taken at bind time.
    """

    class Producer:
        def __init__(self) -> None:
            self.value: dict = {"initial": True}

    producer = Producer()
    binding: LateBinding[dict] = LateBinding("producer_value")
    binding.set(lambda: producer.value)

    assert binding.get() == {"initial": True}

    # Producer rebinds the attribute (not just mutates the existing object).
    producer.value = {"rebound": True}

    assert binding.get() == {"rebound": True}


def test_set_overwrites_previous_reader():
    """Calling set() a second time replaces the bound reader."""
    binding: LateBinding[int] = LateBinding("counter")
    binding.set(lambda: 1)
    binding.set(lambda: 2)

    assert binding.get() == 2


def test_get_reflects_mutation_of_shared_reference():
    """When the reader returns a shared mutable object, mutations are visible."""
    data: dict = {}
    binding: LateBinding[dict] = LateBinding("pending_sync")
    binding.set(lambda: data)

    data["42"] = {"name": "Game"}

    assert binding.get() == {"42": {"name": "Game"}}


def test_error_message_includes_name_and_hint():
    """The RuntimeError text names the binding and points at the cause."""
    binding: LateBinding[dict] = LateBinding("bios_files_index")

    with pytest.raises(RuntimeError) as excinfo:
        binding.get()

    msg = str(excinfo.value)
    assert "bios_files_index" in msg
    assert "set" in msg
    assert "startup ordering bug" in msg
