"""Contract tests: Max skeleton satisfies the BaseModule contract (task 3)."""

import importlib
import inspect
import sys
import threading
import types

import pytest
from base_module import BaseModule, Credential

sys.path.insert(0, ".")

MaxMain = importlib.import_module("Max.main")


class TestSubclass:
    def test_max_is_base_module_subclass(self):
        assert issubclass(MaxMain.Max, BaseModule)


class TestRequiredAttributes:
    def test_unique_id(self):
        assert MaxMain.Max().unique_id == "max_user_1"

    def test_name(self):
        assert MaxMain.Max().name == "MAX"

    def test_description(self):
        desc = MaxMain.Max().description
        assert isinstance(desc, str) and "MAX" in desc and "ToS" in desc

    def test_expected_credentials_five_entries_in_order(self):
        creds = MaxMain.Max.expected_credentials
        names = [c.name for c in creds]
        assert names == [
            "Token",
            "Device ID",
            "Chat ID",
            "Мин. пауза, сек",
            "Макс. пауза, сек",
        ]

    def test_get_creds_returns_five_pairs(self):
        pairs = MaxMain.Max().get_creds()
        assert len(pairs) == 5
        for pair in pairs:
            assert isinstance(pair, dict) and len(pair) == 1

    def test_nested_sender_listener_are_abstract_subclasses(self):
        assert issubclass(MaxMain.Max.Sender, BaseModule.Sender)
        assert issubclass(MaxMain.Max.Listener, BaseModule.Listener)

    def test_stub_bodies_raise_not_implemented(self):
        # Task 4 wired construction (create_session + instantiable nested
        # classes with a session holder); listen BODY still arrives in
        # task 5 and must remain a stub.
        creds = ["tok", "dev", "123", "", ""]
        session = {
            "client": object(),
            "loop": None,
            "chat_id": 123,
            "my_id": 1,
            "stop_event": threading.Event(),
            "min_pause": 2.0,
            "max_pause": 6.0,
        }
        def ingester(text):
            return None

        sender = MaxMain.Max.Sender(creds, "123", session)
        listener = MaxMain.Max.Listener(creds, ingester, "123", session)
        assert listener.ingester is ingester
        assert listener.stop_event is session["stop_event"]
        assert sender.session is session
        assert listener.session is session
        # Task 6 SUPERSEDED the send() stub by design (paced sender):
        # send() now enqueues non-blockingly instead of raising. With no
        # running worker thread it must still return without sending.
        sender._ensure_worker = lambda: None  # pin worker off for stub check
        sender.send("hi")
        assert sender._queue.get_nowait() == "hi"
        with pytest.raises(NotImplementedError):
            listener.listen()


class TestContractEnforcement:
    """How BaseModule.__init_subclass__ + ABC actually reject a bad module.

    Nuance (documented in task-3-failure evidence): the abstract
    @property description in BaseModule shadows the earlier
    `description = None` class attr, so mere OMISSION of description is
    NOT caught by __init_subclass__ — it surfaces at instantiation as an
    abstract-method TypeError. An explicitly FALSY value IS caught at
    class-definition time by __init_subclass__.
    """

    def test_explicitly_falsy_description_rejected_at_class_def(self):
        with pytest.raises(TypeError, match="'description' attribute"):

            class Broken(BaseModule):
                name = "BROKEN"
                description = ""
                expected_credentials = [Credential("Token", "d")]

    def test_omitted_description_breaks_at_instantiation(self):
        class Broken(BaseModule):
            name = "BROKEN"
            expected_credentials = [Credential("Token", "d")]

        with pytest.raises(
            TypeError, match="abstract methods 'description'"
        ):
            Broken()  # module_manager catches this per-module and skips it


class TestDiscovery:
    """Replicates the module_manager CLI loader logic on our own tree."""

    def _discover(self, module_name="Max.main"):
        # Exact replication of module_manager.load() from cryptolayer-cli:
        #   for name, obj in inspect.getmembers(module, inspect.isclass):
        #       if issubclass(obj, BaseModule) and obj is not BaseModule:
        module = importlib.import_module(module_name)
        classes = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, BaseModule) and obj is not BaseModule
        ]
        return module, classes

    def test_discovery_finds_exactly_one_class_and_it_is_Max(self):
        module, classes = self._discover()
        assert classes == [MaxMain.Max]
        instance = classes[0]()
        assert instance.unique_id == "max_user_1"
        assert isinstance(instance, BaseModule)

    def test_discovery_import_does_not_pull_vkmax(self):
        module, _ = self._discover()
        # Lazy-import rule: no module-level `import vkmax` anywhere.
        imported = {
            name
            for name, value in vars(module).items()
            if isinstance(value, types.ModuleType)
        }
        assert not any(name.split(".")[0] == "vkmax" for name in imported)


