import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_demo_audit_matches_actual_output_and_verified_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            result = subprocess.run([sys.executable, "-m", "arma4os", "demo", "--output", str(output)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / "stdout").read_text(), result.stdout)
            lines = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertFalse(lines[0]["performance_evidence"])
            self.assertEqual(lines[-1]["status"]["policy_id"], "default")
            actions = [json.loads(line) for line in (output / "actions.jsonl").read_text().splitlines()]
            self.assertEqual([a["status"] for a in actions], ["applied", "rolled_back"])
            self.assertTrue(all(a["backend"] == "simulated" for a in actions))

    def test_tool_json_errors_and_valid_requests_share_transport(self):
        request = {"schema_version": 1, "request_id": "status", "tool": "scheduler.status", "scope": "global",
                   "timeout_ms": 1000, "params": {}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tools"
            result = subprocess.run([sys.executable, "-m", "arma4os", "tools", "--output", str(output)],
                                    input="not-json\n[]\n" + json.dumps(request) + "\n", capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([line["ok"] for line in lines], [False, False, True])
            self.assertEqual(lines[-1]["result"]["policy_id"], "default")
            requests = output.parent / "requests.jsonl"
            requests.write_text(json.dumps(request) + "\n")
            with requests.open() as stream:
                redirected = subprocess.run([sys.executable, "-m", "arma4os", "tools", "--output", str(output.parent / "redirected")],
                                            stdin=stream, capture_output=True, text=True, timeout=10)
            self.assertEqual(redirected.returncode, 0, redirected.stderr)
            self.assertTrue(json.loads(redirected.stdout)["ok"])

    def test_idle_tool_sigterm_exits_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tools"
            proc = subprocess.Popen([sys.executable, "-m", "arma4os", "tools", "--output", str(output)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                request = {"schema_version": 1, "request_id": "ready", "tool": "scheduler.status", "scope": "global",
                           "timeout_ms": 1000, "params": {}}
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                self.assertTrue(json.loads(proc.stdout.readline())["ok"])
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
                self.assertEqual(proc.returncode, 0, proc.stderr.read())
                self.assertTrue((output / "summary.json").is_file())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
