#!/usr/bin/env python3

import unittest
from unittest import mock
import re
import subprocess

from shared.ioshelper import iOSHelper


BUNDLE_ID = "com.companyname.readwords"
PID = 37238


def event(timestamp, message):
    return {
        "eventType": "logEvent",
        "timestamp": timestamp,
        "eventMessage": message,
    }


class IOSHelperWatchdogTests(unittest.TestCase):
    def test_parses_simulator_time_to_main_and_first_draw(self):
        prefix = f"[app<{BUNDLE_ID}((null))>:{PID}] [realTime]"
        events = [
            event(
                "2026-07-21 18:26:04.438811+0200",
                f"{prefix} Now monitoring resource allowance of 600.00s "
                "(at refreshInterval -1.00s)",
            ),
            event(
                "2026-07-21 18:26:05.995013+0200",
                f"{prefix} Stopped monitoring.",
            ),
            event(
                "2026-07-21 18:26:05.995046+0200",
                f"[app<{BUNDLE_ID}>:{PID}] Provision has remainder",
            ),
            event(
                "2026-07-21 18:26:05.995140+0200",
                f"{prefix} Now monitoring resource allowance of 598.44s "
                "(at refreshInterval -1.00s)",
            ),
            event(
                "2026-07-21 18:26:06.479559+0200",
                f"{prefix} Stopped monitoring.",
            ),
        ]

        self.assertEqual(
            iOSHelper._watchdog_total_ms(events, BUNDLE_ID, PID),
            2040,
        )

    def test_ignores_events_for_another_process(self):
        other_pid = PID + 1
        prefix = f"[app<{BUNDLE_ID}((null))>:{other_pid}] [realTime]"
        events = [
            event(
                "2026-07-21 18:26:04.000000+0200",
                f"{prefix} Now monitoring resource allowance of 600.00s",
            ),
            event(
                "2026-07-21 18:26:05.000000+0200",
                f"{prefix} Stopped monitoring.",
            ),
            event(
                "2026-07-21 18:26:05.000100+0200",
                f"{prefix} Now monitoring resource allowance of 599.00s",
            ),
            event(
                "2026-07-21 18:26:06.000000+0200",
                f"{prefix} Stopped monitoring.",
            ),
        ]

        self.assertEqual(
            iOSHelper._watchdog_total_ms(events, BUNDLE_ID, PID),
            -1,
        )

    def test_rejects_incomplete_sequence(self):
        prefix = f"[app<{BUNDLE_ID}((null))>:{PID}] [realTime]"
        events = [
            event(
                "2026-07-21 18:26:04.000000+0200",
                f"{prefix} Now monitoring resource allowance of 600.00s",
            ),
            event(
                "2026-07-21 18:26:05.000000+0200",
                f"{prefix} Stopped monitoring.",
            ),
        ]

        self.assertEqual(
            iOSHelper._watchdog_total_ms(events, BUNDLE_ID, PID),
            -1,
        )

    def test_log_start_timestamp_includes_timezone(self):
        self.assertRegex(
            iOSHelper._simulator_log_start_timestamp(),
            re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4}$"),
        )

    @mock.patch("shared.ioshelper.subprocess.run")
    def test_log_query_uses_remaining_timeout(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"count":0,"finished":1}\n',
            stderr="",
        )
        helper = iOSHelper()
        helper.device_id = "simulator-udid"

        self.assertEqual(
            helper._read_simulator_watchdog_total(
                BUNDLE_ID,
                PID,
                "2026-07-21 18:26:03+0200",
                2.5,
            ),
            -1,
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 2.5)


if __name__ == "__main__":
    unittest.main()
