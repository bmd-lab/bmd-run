"""R3A: one ``bmd-run batch`` per campaign at a time (the campaign lock).

The concurrency tests run real ``bmd-run`` processes (``python -m bmd_run``) against
the fake BMD Compute on 127.0.0.1. The first process is held inside its first
request to Compute while a second process starts on the same campaign, so the two
genuinely overlap. Nothing contacts a live VM or POWER.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import unittest

from _support import ROOT
from test_batch import NIO, SI, BatchBase

from bmd_run import errors
from bmd_run.attempt_store import CampaignLock, CampaignStore
from bmd_run.errors import LocalStateError

SRC = ROOT / "src"


class Lock(BatchBase):
    def setUp(self):
        super().setUp()
        self.gate_entered = threading.Event()
        self.gate_release = threading.Event()
        self.gated = False

    def tearDown(self):
        self.gate_release.set()
        super().tearDown()

    def hold_first_request(self, method=None):
        """Block the first request (of ``method``, if given) Compute receives until gate_release is set."""

        def gate(received, path, body):
            if not self.gated and method in (None, received):
                self.gated = True
                self.gate_entered.set()
                self.gate_release.wait(60)

        self.compute.observers.append(gate)

    def environ(self):
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.ws.root),
                "BMD_RUN_STATE_DIR": str(self.ws.state), "BMD_RUN_API_URL": self.server.url,
                "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}

    def spawn(self, *argv):
        return subprocess.Popen(
            [sys.executable, "-m", "bmd_run", *argv, "--json", "--token-file", str(self.token_file), "--timeout", "30"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.environ(), cwd=str(self.ws.root))

    def finish(self, process):
        out, err = process.communicate(timeout=90)
        self.assertNotIn(self.token, out + err)
        return process.returncode, json.loads(out) if out.strip() else None, err

    def campaign_files(self, campaign="bench-1"):
        directory = self.ws.state / "campaigns" / campaign
        return sorted(p.name for p in directory.glob("*.json")) if directory.exists() else []

    # --- concurrency ---------------------------------------------------------------

    def test_a_second_process_cannot_advance_a_running_campaign_while_it_plans(self):
        self.check_second_process(None)  # the first process is inside its plan request

    def test_a_second_process_cannot_advance_a_running_campaign_while_it_prepares(self):
        self.check_second_process("PUT")  # the attempt is recorded; Prepare is in flight (Codex's probe)

    def check_second_process(self, gate_method):
        manifest = self.manifest([SI, NIO])
        alias = shutil.copy2(manifest, self.ws.root / "same-campaign-elsewhere.json")  # another path, same campaign
        os.makedirs(self.ws.root / "structures", exist_ok=True)
        for name in ("Si.POSCAR", "NiO.POSCAR"):
            shutil.copy2(self.campaign_dir / "structures" / name, self.ws.root / "structures" / name)
        self.hold_first_request(gate_method)
        first = self.spawn("batch", manifest, "--max-new", "1")
        self.assertTrue(self.gate_entered.wait(60), "the first process reached Compute")
        requests_while_held = len(self.compute.requests)
        records_while_held = self.ws.records()
        for path in (manifest, str(alias)):
            code, document, _ = self.finish(self.spawn("batch", path, "--max-new", "1"))
            self.assertEqual(code, errors.EXIT_LOCAL_STATE, path)
            self.assertEqual(document["error"]["kind"], "local_state_error")
            self.assertIn("another 'bmd-run batch'", document["error"]["message"])
            self.assertEqual(document["cells"], [])
        self.assertEqual(len(self.compute.requests), requests_while_held, "the second process sent nothing")
        self.assertEqual(self.ws.records(), records_while_held, "the second process changed no attempt")
        self.assertEqual(self.campaign_files(), ["si-energy.json"], "the second process recorded nothing")
        self.gate_release.set()
        code, document, err = self.finish(first)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["action"] for r in document["cells"]], ["submitted", "not_started"])
        # --max-new 1 held across both invocations: one attempt in total.
        self.assertEqual((len(self.compute.ledger), self.compute.sbatch_calls), (1, 1))
        self.assertEqual(len(self.ws.records()), 1)
        self.assert_one_attempt_per_cell()

    def test_status_and_other_campaigns_are_usable_while_a_campaign_runs(self):
        manifest = self.manifest([SI])
        other = self.manifest([NIO], campaign="bench-2", name="other.json")
        self.hold_first_request()
        first = self.spawn("batch", manifest)
        self.assertTrue(self.gate_entered.wait(60))
        code, document, _ = self.batch("status", manifest, "--local")
        self.assertEqual((code, [r["category"] for r in document["cells"]]), (0, ["pending"]))
        code, document, _ = self.batch(other)
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["submitted"]))
        self.gate_release.set()
        code, document, err = self.finish(first)
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["submitted"]), err)
        self.assertEqual(self.compute.sbatch_calls, 2)

    def test_a_killed_process_leaves_no_lock(self):
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "from pathlib import Path\n"
             "from bmd_run.attempt_store import CampaignStore\n"
             "with CampaignStore(Path(sys.argv[1])).campaign_lock('bench-1'):\n"
             "    print('locked', flush=True)\n"
             "    time.sleep(120)\n",
             str(self.ws.state)],
            stdout=subprocess.PIPE, text=True, env=self.environ())
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        manifest = self.manifest([SI])
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, document["error"]["kind"]), (errors.EXIT_LOCAL_STATE, "local_state_error"))
        self.assertEqual(self.calls(), [])
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(30)
        holder.stdout.close()
        code, document, _ = self.batch(manifest)
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["submitted"]))

    # --- release -------------------------------------------------------------------

    def assert_unlocked(self, campaign="bench-1"):
        with CampaignStore(self.ws.state).campaign_lock(campaign):
            pass

    def test_released_after_completion_errors_and_interrupts(self):
        manifest = self.manifest([SI, NIO])
        self.assertEqual(self.batch(manifest)[0], 0)
        self.assert_unlocked()
        for fault in (None, None, "active_job_cap_exceeded"):  # NIO: plan, prepare, refused submit
            self.compute.add_fault(fault)
        self.assertEqual(self.batch(manifest)[0], errors.EXIT_QUOTA_EXCEEDED)
        self.assert_unlocked()
        code, _, _ = self.batch(manifest, factory=self.interrupting(
            lambda endpoint, body: endpoint.name == "PUT_ATTEMPT"))
        self.assertEqual(code, 130)
        self.assert_unlocked()
        code, document, _ = self.batch(manifest)  # the lock is free; the interrupted submit stays uncertain
        self.assertEqual((code, [r["action"] for r in document["cells"]]), (0, ["already_submitted", "uncertain_skipped"]))
        self.assertEqual(self.compute.sbatch_calls, 1)
        self.assert_one_attempt_per_cell()

    def test_lock_is_exclusive_within_one_process_and_per_campaign(self):
        store = CampaignStore(self.ws.state)
        with store.campaign_lock("bench-1"):
            with self.assertRaises(LocalStateError):
                with CampaignStore(self.ws.state).campaign_lock("bench-1"):
                    pass
            with store.campaign_lock("bench-2"):
                pass
        self.assert_unlocked()
        lock = self.ws.state / "campaigns" / "bench-1.lock"
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o600)

    def test_the_lock_is_taken_before_campaign_state_is_read_and_held_to_the_end(self):
        from bmd_run import batch, cli
        from bmd_run.api_transport import ApiTransport
        from bmd_run.credentials import load_token

        events = []

        class RecordingLock(CampaignLock):
            def __enter__(inner):
                events.append("lock")
                return CampaignLock.__enter__(inner)

            def __exit__(inner, *exc):
                events.append("unlock")
                return CampaignLock.__exit__(inner, *exc)

        class Recording(CampaignStore):
            def campaign_lock(inner, campaign):
                return RecordingLock(inner, campaign)

            def load_cell(inner, campaign, cell):
                events.append("read")
                return CampaignStore.load_cell(inner, campaign, cell)

        manifest = batch.load_manifest(self.manifest([SI, NIO]), cli._api_request)
        token = load_token(str(self.token_file), {"HOME": str(self.ws.root)})
        api = ApiTransport(self.server.url, token, environ={}, timeout=5)
        self.compute.observers.append(lambda *a: events.append("request"))
        batch.run_batch(api, Recording(self.ws.state), manifest, max_new=2)
        self.assertEqual(events[0], "lock")
        self.assertEqual(events[-1], "unlock")
        self.assertEqual(events.count("lock"), 1)
        self.assertIn("request", events)

    def test_status_takes_no_lock(self):
        manifest = self.manifest([SI])
        self.batch(manifest)
        with CampaignStore(self.ws.state).campaign_lock("bench-1"):
            for argv in (["status", manifest, "--local"], ["status", manifest]):
                code, document, _ = self.batch(*argv)
                self.assertEqual((code, document["cells"][0]["category"]), (0, "submitted"), argv)


if __name__ == "__main__":
    unittest.main()
