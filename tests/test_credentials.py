import unittest

from nass3cp.credentials import CredentialStore, credential_target
from nass3cp.errors import Nass3cpError


class FakeBackend:
    description = "test credential store"

    def __init__(self):
        self.values = {}

    def load(self, target):
        return self.values.get(target)

    def save(self, target, password):
        self.values[target] = password

    def delete(self, target):
        return self.values.pop(target, None) is not None


class CredentialTests(unittest.TestCase):
    def test_target_is_canonical_and_scoped_to_transport_security(self):
        self.assertEqual(
            credential_target("https://NAS.Example:9443"),
            "nass3cp+https://nas.example:9443",
        )
        self.assertEqual(
            credential_target("http://nas.example:9443"),
            "nass3cp+http://nas.example:9443",
        )
        self.assertEqual(
            credential_target("https://nas.example:9443", insecure=True),
            "nass3cp+https-insecure://nas.example:9443",
        )
        self.assertEqual(
            credential_target("https://[2001:DB8::1]:9443"),
            "nass3cp+https://[2001:db8::1]:9443",
        )

    def test_store_delegates_without_exposing_storage_details(self):
        backend = FakeBackend()
        store = CredentialStore(backend=backend)
        target = "nass3cp+https://nas.example:9443"

        self.assertIsNone(store.load(target))
        store.save(target, "secret")
        self.assertEqual(store.load(target), "secret")
        self.assertEqual(store.description, "test credential store")
        self.assertTrue(store.delete(target))
        self.assertFalse(store.delete(target))

    def test_unsupported_platform_can_prompt_but_cannot_claim_to_save(self):
        store = CredentialStore(backend=None)
        self.assertFalse(store.available)
        self.assertIsNone(store.load("target"))
        with self.assertRaises(Nass3cpError):
            store.save("target", "secret")
        with self.assertRaises(Nass3cpError):
            store.delete("target")


if __name__ == "__main__":
    unittest.main()
