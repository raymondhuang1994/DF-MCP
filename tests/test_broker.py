"""Deterministic lifecycle races without launching services or touching sources."""
import contextlib
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from team_drive.broker import Broker
from team_drive.bridge import Bridge
from team_drive.errors import DriveError


class BrokerLifecycleTests(unittest.TestCase):
    def test_other_windows_user_is_rejected_before_config_or_tools(self):
        broker = Broker.__new__(Broker)
        broker.token = "synthetic-credential"
        broker.clients_lock = threading.Lock()
        connection = mock.Mock()
        broker.connections = {connection}
        broker.engine = mock.Mock()
        with mock.patch("team_drive.broker.os.name", "nt"), \
             mock.patch("team_drive.broker.receive", return_value={"type": "hello", "token": broker.token}), \
             mock.patch("team_drive.broker.send") as send, \
             mock.patch("team_drive.windows_identity.require_same_user_pipe", side_effect=DriveError("SERVICE_IDENTITY_MISMATCH", "Different Windows user")):
            broker.handle(connection)
        self.assertEqual(send.call_args.args[1]["error"]["code"], "SERVICE_IDENTITY_MISMATCH")
        broker.engine.call.assert_not_called()
        connection.close.assert_called_once()

    def test_idle_shutdown_sets_stop_while_admission_is_locked(self):
        broker = Broker.__new__(Broker)
        broker.clients_lock = threading.Lock()
        broker.connections = set()
        broker.config = {"idle_seconds": 1}
        broker.last_empty = 0
        broker.stop_event = mock.Mock()
        broker.stop_event.wait.return_value = False
        broker.stop_event.set.side_effect = lambda: self.assertTrue(broker.clients_lock.locked())
        broker.close = mock.Mock()
        with mock.patch("team_drive.broker.threading.Thread"), mock.patch("team_drive.broker.time.monotonic", return_value=2):
            broker.run()
        broker.stop_event.set.assert_called_once_with()
        broker.close.assert_called_once_with()

    def test_accept_after_shutdown_decision_is_closed_without_admission(self):
        broker = Broker.__new__(Broker)
        broker.stop_event = threading.Event()
        broker.clients_lock = threading.Lock()
        broker.connections = set()
        broker.config = {"max_clients": 64}
        connection = mock.Mock()

        def late_accept():
            broker.stop_event.set()
            return connection

        broker.listener = mock.Mock()
        broker.listener.accept.side_effect = late_accept
        with mock.patch("team_drive.broker.threading.Thread") as thread:
            broker.accept()
        connection.close.assert_called_once_with()
        self.assertEqual(broker.connections, set())
        thread.assert_not_called()


class BridgeStartupTests(unittest.TestCase):
    @contextlib.contextmanager
    def environment(self, metadata):
        bridge = Bridge.__new__(Bridge)
        bridge.config = {"root": Path("/source"), "base": Path("/state"), "path": Path("/config.json"),
                         "config_id": "config", "code_id": "code"}
        clock = SimpleNamespace(now=0.0)
        processes = []
        spawn_times = []
        storage = mock.Mock()
        storage.read_metadata.side_effect = lambda name: metadata(name, processes)
        channel = mock.Mock()

        def spawn(*args, **kwargs):
            process = mock.Mock()
            process.poll.return_value = 0
            processes.append(process)
            spawn_times.append(clock.now)
            return process

        def sleep(seconds):
            clock.now += seconds

        fake_time = SimpleNamespace(time=lambda: 1000, monotonic=lambda: clock.now, sleep=sleep)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("team_drive.bridge.SourceAccess"))
            stack.enter_context(mock.patch("team_drive.bridge.StateAccess", return_value=storage))
            stack.enter_context(mock.patch("team_drive.bridge.validate_state_boundaries", return_value=Path("/state")))
            stack.enter_context(mock.patch("team_drive.bridge.time", fake_time))
            stack.enter_context(mock.patch("team_drive.bridge.subprocess.Popen", side_effect=spawn))
            stack.enter_context(mock.patch("team_drive.bridge.connect_endpoint", return_value=channel))
            stack.enter_context(mock.patch("team_drive.bridge.send"))
            receive = stack.enter_context(mock.patch("team_drive.bridge.receive", return_value={"ok": True, "session_id": "session", "instance_id": "owner"}))
            yield bridge, processes, spawn_times, clock, receive

    def test_exited_election_loser_is_retried_with_rate_limit(self):
        def metadata(name, processes):
            return {"token": "credential"} if name == "endpoint.json" and len(processes) >= 2 else None

        with self.environment(metadata) as (bridge, processes, times, clock, receive):
            bridge.connect()
            self.assertEqual(bridge.instance_id, "owner")
            self.assertEqual(len(processes), 2)
            self.assertGreaterEqual(times[1] - times[0], 1)
            self.assertLess(clock.now, 30)

    def test_retries_remain_bounded_by_original_startup_deadline(self):
        with self.environment(lambda name, processes: None) as (bridge, processes, times, clock, receive):
            with self.assertRaises(DriveError) as caught:
                bridge.connect()
            self.assertEqual(caught.exception.code, "SERVICE_START_TIMEOUT")
            self.assertLessEqual(len(processes), 30)
            self.assertLess(clock.now, 30.1)
            self.assertTrue(all(later - earlier >= 1 for earlier, later in zip(times, times[1:])))

    def test_startup_error_is_reported_before_respawn(self):
        def metadata(name, processes):
            if name == "startup-error.json" and processes:
                return {"config_id": "config", "code_id": "code", "time": 1000,
                        "result": {"error": {"code": "UNSAFE_STATE", "message": "Synthetic startup refusal"}}}
            return None

        with self.environment(metadata) as (bridge, processes, times, clock, receive):
            with self.assertRaises(DriveError) as caught:
                bridge.connect()
            self.assertEqual(caught.exception.code, "UNSAFE_STATE")
            self.assertEqual(len(processes), 1)

    def test_configuration_conflict_does_not_spawn_or_retry(self):
        with self.environment(lambda name, processes: {"token": "credential"} if name == "endpoint.json" else None) as (bridge, processes, times, clock, receive):
            receive.return_value = {"ok": False, "error": {"code": "SERVICE_CONFIGURATION_CONFLICT", "message": "Different owner"}}
            with self.assertRaises(DriveError) as caught:
                bridge.connect()
            self.assertEqual(caught.exception.code, "SERVICE_CONFIGURATION_CONFLICT")
            self.assertEqual(processes, [])

    def test_changed_endpoint_after_auth_failure_retries_only_handshake(self):
        old_endpoint = {"token": "a" * 64, "instance_id": "old"}
        new_endpoint = {"token": "b" * 64, "instance_id": "new"}
        endpoints = iter([old_endpoint, new_endpoint, new_endpoint])

        def metadata(name, processes):
            self.assertEqual(name, "endpoint.json")
            return next(endpoints)

        old_channel, new_channel = mock.Mock(), mock.Mock()
        with self.environment(metadata) as (bridge, processes, times, clock, receive):
            receive.side_effect = [{"ok": False, "error": {"code": "SERVICE_AUTH_FAILED", "message": "Old credential"}},
                                   {"ok": True, "session_id": "session", "instance_id": "new"}]
            with mock.patch("team_drive.bridge.connect_endpoint", side_effect=[old_channel, new_channel]) as connect, mock.patch("team_drive.bridge.send") as send:
                bridge.connect()
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(bridge.instance_id, "new")
            self.assertIs(bridge.connection, new_channel)
            self.assertEqual([call.args[1]["type"] for call in send.call_args_list], ["hello", "hello"])
            self.assertEqual([call.args[1]["token"] for call in send.call_args_list], [old_endpoint["token"], new_endpoint["token"]])
            old_channel.close.assert_called_once_with()
            new_channel.close.assert_not_called()
            self.assertEqual(processes, [])

    def test_unchanged_endpoint_auth_failure_remains_explicit(self):
        endpoint = {"token": "a" * 64, "instance_id": "same"}
        with self.environment(lambda name, processes: endpoint) as (bridge, processes, times, clock, receive):
            receive.return_value = {"ok": False, "error": {"code": "SERVICE_AUTH_FAILED", "message": "Invalid credential"}}
            with self.assertRaises(DriveError) as caught:
                bridge.connect()
            self.assertEqual(caught.exception.code, "SERVICE_AUTH_FAILED")
            self.assertEqual(receive.call_count, 1)
            self.assertEqual(processes, [])

    def test_endpoint_rotation_does_not_extend_startup_deadline(self):
        def metadata(name, processes):
            self.assertEqual(name, "endpoint.json")
            clock.now += 5
            return {"token": str(int(clock.now)).zfill(64), "instance_id": str(clock.now)}

        with self.environment(metadata) as (bridge, processes, times, clock, receive):
            receive.return_value = {"ok": False, "error": {"code": "SERVICE_AUTH_FAILED", "message": "Rotated credential"}}
            with self.assertRaises(DriveError) as caught:
                bridge.connect()
            self.assertEqual(caught.exception.code, "SERVICE_START_TIMEOUT")
            self.assertEqual(clock.now, 30)
            self.assertEqual(receive.call_count, 3)
            self.assertEqual(processes, [])


if __name__ == "__main__":
    unittest.main()
