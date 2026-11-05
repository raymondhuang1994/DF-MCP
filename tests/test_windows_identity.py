"""Same-user named-pipe gate contracts; fake native API outside Windows."""
import os
import unittest
from unittest import mock

from team_drive.errors import DriveError
from team_drive import windows_identity


class PipeIdentityTests(unittest.TestCase):
    def api(self):
        api = mock.Mock()
        api.client_pid.return_value = 101
        api.open_process.return_value = 501
        api.current_process.return_value = -1
        api.process_sid.return_value = "S-1-5-21-1000"
        return api

    def test_same_user_accepted_and_client_handle_closed(self):
        api = self.api()
        with mock.patch.object(windows_identity, "_WindowsAPI", return_value=api):
            windows_identity.require_same_user_pipe(123)
        api.open_process.assert_called_once_with(101)
        api.close_process.assert_called_once_with(501)
        self.assertEqual(api.client_pid.call_count, 2)

    def test_different_user_rejected_even_with_valid_ipc_connection(self):
        api = self.api()
        api.process_sid.side_effect = ["S-1-5-21-2000", "S-1-5-21-1000"]
        with mock.patch.object(windows_identity, "_WindowsAPI", return_value=api):
            with self.assertRaises(DriveError) as caught:
                windows_identity.require_same_user_pipe(123)
        self.assertEqual(caught.exception.code, "SERVICE_IDENTITY_MISMATCH")
        self.assertNotIn("2000", caught.exception.message)
        api.close_process.assert_called_once_with(501)

    def test_pid_race_and_unavailable_token_fail_closed(self):
        for failure in ("pid", "token", "open", "race"):
            api = self.api()
            if failure == "race":
                api.client_pid.side_effect = [101, 202]
            else:
                getattr(api, {"pid": "client_pid", "token": "process_sid", "open": "open_process"}[failure]).side_effect = OSError("PRIVATE_NATIVE_DETAIL")
            with self.subTest(failure=failure), mock.patch.object(windows_identity, "_WindowsAPI", return_value=api):
                with self.assertRaises(DriveError) as caught:
                    windows_identity.require_same_user_pipe(123)
            self.assertEqual(caught.exception.code, "SERVICE_IDENTITY_UNVERIFIED")
            self.assertNotIn("PRIVATE", caught.exception.message)
            self.assertEqual(api.close_process.call_count, int(failure in ("token", "race")))

    @unittest.skipUnless(os.name == "nt", "Native Windows user token")
    def test_native_current_user_binding_is_stable_and_opaque(self):
        first = windows_identity.principal_binding()
        self.assertEqual(first, windows_identity.principal_binding())
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    @unittest.skipUnless(os.name == "nt", "Real Windows named-pipe identity API")
    def test_native_connected_named_pipe_accepts_current_user(self):
        # multiprocessing.Pipe uses an actual connected Windows named pipe;
        # no listener thread, external server or administrator rights required.
        from multiprocessing import Pipe
        server, client = Pipe(duplex=True)
        try:
            windows_identity.require_same_user_pipe(server.fileno())
        finally:
            client.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
