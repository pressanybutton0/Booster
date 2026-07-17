"""Regression tests for the asynchronous get-up chassis gate."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


def _install_boosteros_import_stub() -> None:
    """Provide only the SDK names needed to import RobotClient in local Python."""

    try:
        __import__("boosteros.robots.booster")
        return
    except ModuleNotFoundError:
        pass

    boosteros = types.ModuleType("boosteros")
    robots = types.ModuleType("boosteros.robots")
    booster = types.ModuleType("boosteros.robots.booster")
    booster.BoosterRobot = object
    booster.RobotGaitName = str
    booster.RobotModeName = str
    booster.SoccerKickManager = object
    sys.modules["boosteros"] = boosteros
    sys.modules["boosteros.robots"] = robots
    sys.modules["boosteros.robots.booster"] = booster


_install_boosteros_import_stub()

from src.soccer_framework import MoveIntent, RobotCommand, RobotRuntimeStatus, SoccerConfig
from src.soccer_framework.robot import RobotClient


class _Logger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def warn(self, *_args: object, **_kwargs: object) -> None:
        pass


class _FallState:
    state = "fallen"
    recoverable = True


class _Robot:
    def __init__(self) -> None:
        self.mode = "damping"
        self.get_up_calls = 0
        self.get_up_error: Exception | None = None
        self.velocities: list[tuple[float, float, float]] = []

    def get_mode(self) -> str:
        return self.mode

    def get_fall_down_state(self) -> _FallState:
        return _FallState()

    def get_up(self) -> None:
        self.get_up_calls += 1
        if self.get_up_error is not None:
            raise self.get_up_error

    def set_velocity(self, *, vx: float, vy: float, vyaw: float) -> None:
        self.velocities.append((vx, vy, vyaw))


class _KickManager:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.stop_error: Exception | None = None

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error


def _client() -> tuple[RobotClient, _Robot]:
    client = object.__new__(RobotClient)
    robot = _Robot()
    client._player_id = 1
    client._robot_name = "robot1"
    client._robot = robot
    client._kick_manager = _KickManager()
    client._config = SoccerConfig()
    client._logger = _Logger()
    client._cached_status = RobotRuntimeStatus()
    client._last_get_up_at = 0.0
    client._get_up_retry_after = 0.0
    client._get_up_failure_count = 0
    client._get_up_settle_until = 0.0
    client._last_walk_mode_attempt_at = 0.0
    client._unexpected_non_walk_since = 0.0
    client._kick_enabled = False
    client._kick_started_at = 0.0
    client._kick_stop_retry_after = 0.0
    return client, robot


class GetUpRecoveryTests(unittest.TestCase):
    def test_failed_get_up_uses_exponential_retry_backoff(self) -> None:
        client, robot = _client()
        robot.get_up_error = RuntimeError("Failed to start get_up")
        client._cached_status = RobotRuntimeStatus(
            mode="damping",
            fall_down_state="fallen",
            fall_down_recoverable=True,
        )

        self.assertFalse(client.trigger_get_up(10.0))
        self.assertEqual(robot.get_up_calls, 1)
        self.assertFalse(client.trigger_get_up(11.9))
        self.assertEqual(robot.get_up_calls, 1)

        self.assertFalse(client.trigger_get_up(12.0))
        self.assertEqual(robot.get_up_calls, 2)
        self.assertFalse(client.trigger_get_up(15.9))
        self.assertEqual(robot.get_up_calls, 2)

        self.assertFalse(client.trigger_get_up(16.0))
        self.assertEqual(robot.get_up_calls, 3)

    def test_get_up_releases_active_kick_first(self) -> None:
        client, robot = _client()
        client._cached_status = RobotRuntimeStatus(
            mode="damping",
            fall_down_state="fallen",
            fall_down_recoverable=True,
        )
        client._kick_enabled = True

        self.assertTrue(client.trigger_get_up(10.0))
        self.assertEqual(client._kick_manager.stop_calls, 1)
        self.assertEqual(robot.get_up_calls, 1)

    def test_still_fallen_robot_can_retry_before_incapable_window(self) -> None:
        client, robot = _client()

        self.assertEqual(client.poll_status(10.0).fall_down_state, "fallen")
        self.assertTrue(client.trigger_get_up(10.0))
        self.assertEqual(robot.get_up_calls, 1)

        # If the first asynchronous task did not recover the body, a second
        # attempt starts at 6.5s instead of waiting until the 10s referee limit.
        self.assertEqual(client.poll_status(16.5).fall_down_state, "fallen")
        self.assertTrue(client.trigger_get_up(16.5))
        self.assertEqual(robot.get_up_calls, 2)

    def test_walk_mode_recovery_is_blocked_while_fallen(self) -> None:
        client, _robot = _client()
        client._cached_status = RobotRuntimeStatus(
            mode="damping",
            fall_down_state="fallen",
            fall_down_recoverable=True,
        )
        calls: list[str] = []
        client._enter_soccer_mode = calls.append  # type: ignore[method-assign]

        client.ensure_walk_mode("test")

        self.assertEqual(calls, [])

    def test_unexpected_damping_waits_for_fall_sensor_before_walk_recovery(self) -> None:
        client, robot = _client()
        robot.mode = "walk"
        client.poll_status(9.0)
        robot.mode = "damping"
        client.poll_status(10.0)
        # Model the observed SDK window: damping arrives before the fall sensor
        # changes from normal to fallen.
        client._cached_status = RobotRuntimeStatus(
            mode="damping",
            fall_down_state="normal",
            fall_down_recoverable=False,
            updated_at=10.0,
        )
        calls: list[str] = []
        client._enter_soccer_mode = calls.append  # type: ignore[method-assign]

        with patch("src.soccer_framework.robot.time.monotonic", return_value=11.7):
            client.ensure_walk_mode("transient damping")
        self.assertEqual(calls, [])

        with patch("src.soccer_framework.robot.time.monotonic", return_value=11.8):
            client.ensure_walk_mode("persistent damping")
        self.assertEqual(calls, ["persistent damping"])

    def test_walk_mode_does_not_release_chassis_during_get_up(self) -> None:
        client, robot = _client()

        self.assertEqual(client.poll_status(10.0).fall_down_state, "fallen")
        self.assertTrue(client.trigger_get_up(10.0))
        self.assertEqual(robot.get_up_calls, 1)

        # SoccerSim reports walk before its asynchronous get_up task releases
        # the chassis. The local gate must remain authoritative until 6.5s.
        robot.mode = "walk"
        status = client.poll_status(11.0)
        self.assertEqual(status.mode, "walk")
        self.assertEqual(status.fall_down_state, "getting_up")
        self.assertFalse(client.trigger_get_up(11.0))
        self.assertEqual(robot.get_up_calls, 1)

        client.apply(RobotCommand(intent=MoveIntent(vx=0.2), reason="test move"))
        client.apply(RobotCommand.stop("test stop"))
        self.assertEqual(robot.velocities, [])

        self.assertEqual(client.poll_status(16.5).fall_down_state, "normal")
        client.apply(RobotCommand(intent=MoveIntent(vx=0.2), reason="test move"))
        self.assertEqual(robot.velocities, [(0.2, 0.0, 0.0)])

    def test_failed_kick_stop_keeps_chassis_owned_until_retry_succeeds(self) -> None:
        client, robot = _client()
        client._cached_status = RobotRuntimeStatus(
            mode="walk",
            fall_down_state="normal",
        )
        client._kick_enabled = True
        client._kick_started_at = 1.0
        kick_manager = client._kick_manager
        assert isinstance(kick_manager, _KickManager)
        kick_manager.stop_error = RuntimeError("API call failed, code = 400")

        with patch("src.soccer_framework.robot.time.monotonic", return_value=10.0):
            client.apply(RobotCommand.stop("enter SET"))

        self.assertTrue(client._kick_enabled)
        self.assertEqual(kick_manager.stop_calls, 1)
        self.assertEqual(robot.velocities, [])

        # The 30Hz control loop must not turn a rejected stop into an RPC storm.
        with patch("src.soccer_framework.robot.time.monotonic", return_value=10.2):
            client.apply(RobotCommand.stop("enter SET"))
        self.assertEqual(kick_manager.stop_calls, 1)

        kick_manager.stop_error = None
        with patch("src.soccer_framework.robot.time.monotonic", return_value=10.5):
            client.apply(RobotCommand.stop("enter SET"))

        self.assertFalse(client._kick_enabled)
        self.assertEqual(kick_manager.stop_calls, 2)
        self.assertEqual(robot.velocities, [(0.0, 0.0, 0.0)])


if __name__ == "__main__":
    unittest.main()
