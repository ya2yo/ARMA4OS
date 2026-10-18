"""Real backend lifecycle with a fake bpftool; never attaches to the host kernel."""
import tempfile
import unittest
from pathlib import Path

from arma4os.bpf_hook import BpfCounterHook


class FakeBpf(BpfCounterHook):
    def __init__(self, directory):
        root = Path(directory)
        obj = root / "counter.o"
        obj.touch()
        super().__init__({"service": root}, obj, root / "pins", Path("/bin/true"))
        self.entries = []
        self.fail_attach = False

    def command(self, *args, json_output=False):
        if args[:2] == ("cgroup", "show"):
            return self.entries[:]
        if args[:2] == ("prog", "loadall"):
            Path(args[3]).mkdir()
            (Path(args[3]) / "count_ingress").touch()
            (Path(args[-1]) / "counters").touch()
        elif args[:2] == ("prog", "show"):
            return {"id": 42}
        elif args[:2] == ("cgroup", "attach"):
            if self.fail_attach:
                raise RuntimeError("injected attach failure")
            self.entries = [{"id": 42, "attach_type": "ingress"}]
        elif args[:2] == ("cgroup", "detach"):
            self.entries = [e for e in self.entries if e["id"] != 42]
        elif args[:2] == ("map", "dump"):
            return [{"key": 0, "values": [{"cpu": 0, "value": {"packets": 1, "bytes": 100}}]}]
        return ""


class BpfLifecycleTests(unittest.TestCase):
    def test_kernel_id_verified_owned_detach_and_pin_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            hook = FakeBpf(directory)
            self.assertTrue(hook.attach("service", {}, dry_run=True)["dry_run"])
            self.assertFalse(hook.managed)
            stats = hook.attach("service", {})
            self.assertTrue(stats["attached"])
            self.assertEqual(stats["program_id"], 42)
            hook.close()
            self.assertEqual(hook.entries, [])
            self.assertFalse(hook.managed)
            self.assertEqual(list(hook.pin_root.iterdir()), [])

    def test_external_ingress_refused_and_failed_attach_removes_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            hook = FakeBpf(directory)
            hook.entries = [{"id": 99, "attach_type": "ingress"}]
            with self.assertRaises(ValueError):
                hook.attach("service", {})
            self.assertEqual(hook.entries[0]["id"], 99)
            hook.entries.clear()
            hook.fail_attach = True
            with self.assertRaises(RuntimeError):
                hook.attach("service", {})
            self.assertFalse(hook.managed)
            self.assertEqual(list(hook.pin_root.iterdir()), [])

    def test_unauthorized_scope_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            hook = FakeBpf(directory)
            # Fake command implementation does not handle authorization itself;
            # the real attached() method must check it before invoking bpftool.
            with self.assertRaises(ValueError):
                hook.attach("other", {})


if __name__ == "__main__":
    unittest.main()
