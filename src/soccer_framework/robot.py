"""Hardware-facing wrappers for boosteros robots.

:class:`RobotClient` controls one robot. It owns hardware access, status
snapshots, kick/velocity exclusivity, get-up retry, mode recovery, and all
per-robot state. :class:`TeamRobotManager` handles routing plus team lifecycle
and forwards by ``player_id`` to each client.

To swap hardware backends, change only :class:`RobotClient` private ``_``
hardware-access methods.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, cast

from boosteros.robots.booster import (
    BoosterRobot,
    RobotGaitName,
    RobotModeName,
    SoccerKickManager,
)

from .config import SoccerConfig
from .types import (
    KickIntent,
    MoveIntent,
    NoopIntent,
    RobotCommand,
    RobotRuntimeStatus,
    StopIntent,
)


__all__ = ["RobotClient", "TeamRobotManager"]


class _RobotCloser(Protocol):
    def __call__(self) -> None: ...


class RobotClient:
    """Control client for one robot: hardware access plus all stateful per-robot logic.

    Everything about driving one robot lives here:

    Lifecycle**: construction opens hardware, ``start`` logs initialization,
    and ``close`` shuts hardware down.
    Status polling**: ``poll_status`` reads SDK mode each tick; fall_down is
    read only outside walk mode, and ``ensure_walk_mode`` is confirmed by the next tick.
    Safety actions**: ``trigger_get_up`` is retry-throttled, and
    ``ensure_walk_mode`` throttles transition requests and waits for a polled confirmation.
    Command execution**: ``apply`` routes :class:`RobotCommand`; kick intents
    own the chassis, move commands wait for kick release, stop forces release and
    zero velocity, and no-op leaves hardware untouched.

    The chassis velocity channel is exclusive: either kicking or ``set_velocity``
    owns it. ``_kick_enabled`` and ``_kick_started_at`` track kick state, and
    ``_release_kick`` returns False during minimum active time so move commands skip
    this tick instead of flapping start/stop at the kick boundary.
    """

    _GET_UP_RETRY_INTERVAL_SEC = 1.0
    # A rejected get_up RPC usually means the simulator's motion service is
    # still busy. Retrying it every control second only adds more competing
    # requests. Back off failed starts while continuing to hold StopIntent.
    _GET_UP_FAILURE_RETRY_BASE_SEC = 2.0
    _GET_UP_FAILURE_RETRY_MAX_SEC = 8.0
    # get_up() starts an asynchronous task. The SDK can report ``walk`` while
    # that task still owns the chassis, so mode alone cannot safely release
    # velocity commands. Most SoccerSim recoveries complete in about 5.8s, but
    # the latest collision run still had an accepted task active after 6.5s.
    # Keep an 8s ownership window so a second get_up cannot overlap it.
    _GET_UP_SETTLE_INTERVAL_SEC = 8.0
    _WALK_MODE_RETRY_INTERVAL_SEC = 1.0
    # When a walking robot falls, mode changes to damping slightly before the
    # fall_down state becomes "fallen". Give that sensor a short window before
    # treating damping as an ordinary mode-loss and calling set_mode(walk).
    _UNEXPECTED_NON_WALK_GRACE_SEC = 1.75

    def __init__(
        self,
        player_id: int,
        robot_name: str,
        config: SoccerConfig,
        logger: Any,
    ):
        self._player_id = player_id
        self._robot_name = robot_name
        self._robot = BoosterRobot(
            virtual_robot_name=robot_name,
            enable_tf_listener=False,
            timeout=10.0,
        )
        self._kick_manager = SoccerKickManager(self._robot)
        self._config = config
        self._logger = logger

        # Status polling cache
        self._cached_status: RobotRuntimeStatus = RobotRuntimeStatus()

        # Get-up retry throttle
        self._last_get_up_at = 0.0
        self._get_up_retry_after = 0.0
        self._get_up_failure_count = 0
        self._get_up_settle_until = 0.0
        self._last_walk_mode_attempt_at = 0.0
        self._unexpected_non_walk_since = 0.0

        # Kick state machine
        self._kick_enabled = False
        self._kick_started_at = 0.0

    # Identity

    @property
    def player_id(self) -> int:
        return self._player_id

    @property
    def robot_name(self) -> str:
        return self._robot_name

    @property
    def display_name(self) -> str:
        return self._robot_name or "<default>"

    # Lifecycle: start to close
    #
    # Construction already opens hardware; ``start`` only handles runtime registration and init logs.
    # ``close`` exits active state and releases resources. The pair is symmetric and self-contained.
    # Callers do not need to stop commands before ``close``. Runtime per-tick actions use :meth:`apply`,
    # and stop is ``apply(RobotCommand.stop(reason))``.

    def start(self) -> None:
        """Lifecycle start: log initialization and do not actively switch soccer/walk mode.

        Paired with :meth:`close`. Hardware is already connected; real mode
        recovery is triggered by :meth:`ensure_walk_mode` from non-stop READY/PLAYING commands.
        """

        self._logger.info(
            f"Initialized {self.display_name} as "
            f"{self._config.ready_slot_for_player(self._player_id).value}",
            event="robot_initialized",
            team_id=self._config.team_id,
            player_id=self._player_id,
            robot_name=self.display_name,
            ready_slot=self._config.ready_slot_for_player(self._player_id).value,
        )

    def close(self) -> None:
        """Lifecycle close: release kick, send zero velocity, and close hardware.

        Full counterpart to :meth:`start`: stop all current actions first, forcing
        kick release and zero velocity, then close hardware. Callers need not pre-stop.
        """

        self.poll_status(time.monotonic())
        self.apply(RobotCommand.stop("runtime closing"))
        try:
            self._close_hardware()
        except Exception as exc:
            self._logger.warn(
                f"robot close failed: {exc}",
                event="robot_close_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                error=str(exc),
            )

    # Status polling for BT DataLayer

    def poll_status(self, now: float) -> RobotRuntimeStatus:
        """Read a hardware-status snapshot; safe for BT to call every tick.

        Mode is important and can be changed externally, so read the SDK cache each
        tick to avoid sending velocity in prepare/damping. fall_down_state is read only outside walk mode.
        """

        previous = self._cached_status
        mode = previous.mode
        fall_down_state = previous.fall_down_state
        fall_down_recoverable = previous.fall_down_recoverable
        updated = False

        polled_mode = self._poll_mode()
        if polled_mode is not None:
            mode = polled_mode
            updated = True
            if mode == "walk":
                self._unexpected_non_walk_since = 0.0
            elif previous.mode == "walk":
                self._unexpected_non_walk_since = now

        if now < self._get_up_settle_until:
            # get_up() is asynchronous and get_mode() may already say walk even
            # though the recovery task still rejects set_velocity. Preserve a
            # non-normal state so SafetyOverrides keeps writing StopIntent.
            fall_down_state = "getting_up"
            fall_down_recoverable = False
            updated = True
        elif mode == "walk":
            fall_down_state = "normal"
            fall_down_recoverable = False
        elif mode in {"prepare", "damping"}:
            result = self._poll_fall_down_state()
            if result is not None:
                fall_down_state, fall_down_recoverable = result
                updated = True

        status = RobotRuntimeStatus(
            mode=mode,
            fall_down_state=fall_down_state,
            fall_down_recoverable=fall_down_recoverable,
            updated_at=now if updated else previous.updated_at,
        )
        self._cached_status = status
        return status

    # Safety actions for BT SafetyOverrides

    def trigger_get_up(self, now: float) -> bool:
        """Send ``get_up()`` at most once per retry interval; return whether a command was actually sent."""

        if now < self._get_up_settle_until or now < self._get_up_retry_after:
            return False
        if now - self._last_get_up_at < self._GET_UP_RETRY_INTERVAL_SEC:
            return False
        self._last_get_up_at = now

        state = self._cached_status.fall_down_state or "unknown"
        # A fall can interrupt an active kick. Release that chassis owner before
        # asking the SDK to start its asynchronous get-up task.
        self._release_kick(force=True, reason="fall down recovery")
        try:
            self._get_up()
        except Exception as exc:
            if "get_up" in str(exc) and "already running" in str(exc):
                self._mark_get_up_settling(now)
                self._logger.info(
                    f"Get-up already running for player {self._player_id}; "
                    "holding chassis commands",
                    event="get_up_already_running",
                    team_id=self._config.team_id,
                    player_id=self._player_id,
                    state=state,
                )
                return False
            self._get_up_failure_count += 1
            retry_delay = min(
                self._GET_UP_FAILURE_RETRY_BASE_SEC
                * (2 ** (self._get_up_failure_count - 1)),
                self._GET_UP_FAILURE_RETRY_MAX_SEC,
            )
            self._get_up_retry_after = now + retry_delay
            self._logger.warn(
                f"get_up failed for player {self._player_id}: "
                f"{exc.__class__.__name__}: {exc}; retry in {retry_delay:.1f}s",
                event="get_up_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                state=state,
                failure_count=self._get_up_failure_count,
                retry_delay_sec=retry_delay,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            return False

        self._mark_get_up_settling(now)
        self._logger.info(
            f"Getting up player {self._player_id}: state={state}",
            event="get_up_started",
            team_id=self._config.team_id,
            player_id=self._player_id,
            state=state,
        )
        return True

    def _mark_get_up_settling(self, now: float) -> None:
        """Keep the chassis blocked until the asynchronous get-up task settles."""

        self._get_up_failure_count = 0
        self._get_up_retry_after = 0.0
        self._get_up_settle_until = max(
            self._get_up_settle_until,
            now + self._GET_UP_SETTLE_INTERVAL_SEC,
        )
        previous = self._cached_status
        self._cached_status = RobotRuntimeStatus(
            mode=previous.mode,
            fall_down_state="getting_up",
            fall_down_recoverable=False,
            updated_at=now,
        )

    def ensure_walk_mode(self, reason: str) -> None:
        """Request walk gait/mode and hold movement until polling confirms it."""

        now = time.monotonic()
        # Get-up owns the chassis. This local guard remains authoritative even
        # if a stale BT command or a transient get_mode failure asks for walk.
        if now < self._get_up_settle_until or not self._cached_status.is_fall_down_normal:
            return
        if (
            self._unexpected_non_walk_since > 0.0
            and now - self._unexpected_non_walk_since
            < self._UNEXPECTED_NON_WALK_GRACE_SEC
        ):
            return
        if now - self._last_walk_mode_attempt_at < self._WALK_MODE_RETRY_INTERVAL_SEC:
            return
        self._last_walk_mode_attempt_at = now

        previous = self._cached_status
        self._logger.info(
            f"Recovering walk mode for player {self._player_id}: "
            f"mode={previous.mode}, reason={reason}",
            event="walk_mode_recovery",
            team_id=self._config.team_id,
            player_id=self._player_id,
            robot_name=self.display_name,
            mode=previous.mode,
            reason=reason,
        )
        # A successful RPC only acknowledges the requested transition; the
        # robot may still reject velocity for several control ticks.  Keep the
        # observed non-walk cache unchanged so SafetyOverrides continues to
        # replace movement with stop until poll_status actually reports walk.
        self._enter_soccer_mode(reason)

    # Command execution

    def apply(self, command: RobotCommand) -> None:
        """Execute one :class:`RobotCommand`; each intent takes its own path.

        :class:`KickIntent` starts/updates the kick channel and owns the chassis;
        :class:`StopIntent` stops kick then zeroes velocity; :class:`NoopIntent` skips
        hardware; :class:`MoveIntent` releases kick with ``min_active`` debounce before velocity.

        Whether stop should touch velocity is decided upstream from this tick's
        mode. This method only executes the already-dispatched intent to avoid repeated ``get_mode``.
        """

        intent = command.intent
        if (
            not self._cached_status.is_fall_down_normal
            and not isinstance(intent, (StopIntent, NoopIntent))
        ):
            self._apply_stop("fall down recovery")
            return
        if isinstance(intent, KickIntent):
            self._apply_kick(intent, reason=command.reason)
            return
        if isinstance(intent, StopIntent):
            self._apply_stop(command.reason)
            return
        if isinstance(intent, NoopIntent):
            return
        # MoveIntent
        self._apply_move(intent, command.reason)

    def _apply_stop(self, reason: str) -> None:
        """Stop command: if kicking, run ``stop_kick`` first, then ``set_velocity(0,0,0)``.

        Two clear cases:

        Normal walking returns immediately from ``_release_kick`` and sends zero
        velocity; active kick force-stops the kick first, then sends zero velocity.

        Stop always forces kick release; ``min_active`` debounce only protects
        :meth:`_apply_move` against kick/move boundary flapping.
        """

        self._release_kick(force=True, reason=reason)
        if (
            self._cached_status.is_fall_down_normal
            and self._cached_status.mode in {None, "walk"}
        ):
            self._dispatch_velocity(MoveIntent(), reason)

    def _apply_move(self, intent: MoveIntent, reason: str) -> None:
        """Move command: release kick with ``min_active`` debounce before sending velocity.

        If kick is still inside minimum active duration, chassis release fails and
        this tick skips ``set_velocity`` to avoid repeated kick start/stop.
        """

        if not self._release_kick(force=False, reason=reason):
            return
        self._dispatch_velocity(intent, reason)

    # Private hardware access, forwarded from the old PlayerRobot under _ prefixes

    def _set_velocity(self, *, vx: float, vy: float, vyaw: float) -> None:
        self._robot.set_velocity(vx=vx, vy=vy, vyaw=vyaw)

    def _set_mode(self, mode: RobotModeName) -> None:
        self._robot.set_mode(mode)

    def _set_gait(self, gait: RobotGaitName) -> None:
        self._robot.set_gait(gait)

    def _list_gaits(self) -> list[str]:
        gaits: object = self._robot.list_gaits()
        if not isinstance(gaits, list):
            return []
        return [gait for gait in gaits if isinstance(gait, str)]

    def _get_mode(self) -> object:
        return self._robot.get_mode()

    def _get_up(self) -> None:
        self._robot.get_up()

    def _get_fall_down_state(self) -> object:
        return self._robot.get_fall_down_state()

    def _start_kick(self) -> None:
        self._kick_manager.start()

    def _stop_kick(self) -> None:
        self._kick_manager.stop()

    def _update_kick_command(self, *, direction: float, power: float) -> None:
        self._kick_manager.update_command(direction=direction, power=power)

    def _update_kick_ball(self, *, x: float, y: float) -> None:
        self._kick_manager.update_ball(x=x, y=y)

    def _close_hardware(self) -> None:
        close_fn: object = getattr(self._robot, "_close", None)
        if callable(close_fn):
            cast(_RobotCloser, close_fn)()

    # Private kick lifecycle

    def _apply_kick(self, intent: KickIntent, reason: str) -> None:
        """Handle one :class:`KickIntent`; while owning the chassis, only update kick command."""

        try:
            if not self._kick_enabled:
                self._start_kick()
                self._kick_enabled = True
                self._kick_started_at = time.monotonic()
                self._logger.info(
                    "SoccerKickManager started",
                    event="soccer_kick_started",
                    console=False,
                    team_id=self._config.team_id,
                    player_id=self._player_id,
                    direction=round(intent.direction, 3),
                    power=round(max(1.0, min(10.0, intent.power)), 3),
                    ball_x=round(intent.ball_x, 3),
                    ball_y=round(intent.ball_y, 3),
                    reason=reason,
                )
            self._update_kick_command(
                direction=intent.direction,
                power=max(1.0, min(10.0, intent.power)),
            )
            self._update_kick_ball(x=intent.ball_x, y=intent.ball_y)
        except Exception as exc:
            self._logger.warn(
                f"SoccerKickManager update failed for player {self._player_id}: "
                f"{exc.__class__.__name__}: {exc}",
                event="soccer_kick_update_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                error_type=exc.__class__.__name__,
                error=str(exc),
                reason=reason,
            )
            self._kick_enabled = False

    def _release_kick(self, *, force: bool, reason: str) -> bool:
        """Release chassis control from kicking; return False while still inside minimum active duration."""

        if not self._kick_enabled:
            return True
        active_for = time.monotonic() - self._kick_started_at
        if not force and active_for < self._config.strategy.soccer_kick_min_active_sec:
            return False
        self._logger.info(
            f"Stopping SoccerKickManager for player {self._player_id}: "
            f"reason={reason}, active_for={active_for:.2f}s, force={force}",
        )
        try:
            self._stop_kick()
            self._logger.info(
                "SoccerKickManager stopped",
                event="soccer_kick_stopped",
                console=False,
                team_id=self._config.team_id,
                player_id=self._player_id,
                reason=reason,
                active_for_sec=round(active_for, 3),
                force=force,
            )
        except Exception as exc:
            self._logger.warn(
                f"SoccerKickManager stop failed for player {self._player_id}: "
                f"{exc.__class__.__name__}: {exc}",
                event="soccer_kick_stop_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                reason=reason,
                active_for_sec=round(active_for, 3),
                force=force,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
        finally:
            self._kick_enabled = False
        return True

    # Private status snapshot polling

    def _poll_fall_down_state(self) -> tuple[str | None, bool] | None:
        try:
            fall_down_state = self._get_fall_down_state()
        except Exception as exc:
            self._logger.warn(
                f"get_fall_down_state failed for player {self._player_id}: "
                f"{exc.__class__.__name__}: {exc}",
                event="get_fall_down_state_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            return None
        state_value = getattr(fall_down_state, "state", None)
        state = state_value if isinstance(state_value, str) else None
        recoverable_value = getattr(fall_down_state, "recoverable", False)
        recoverable = (
            recoverable_value if isinstance(recoverable_value, bool) else False
        )
        return state, recoverable

    def _poll_mode(self) -> str | None:
        try:
            mode = self._get_mode()
        except Exception as exc:
            self._logger.warn(
                f"get_mode failed for player {self._player_id}: "
                f"{exc.__class__.__name__}: {exc}",
                event="get_mode_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            return None
        return mode if isinstance(mode, str) else None

    # Private mode switching

    def _enter_soccer_mode(self, reason: str) -> bool:
        try:
            self._set_gait("soccer")
            self._set_mode("walk")
        except Exception as exc:
            self._logger.warn(
                f"{self.display_name} enter soccer mode failed: "
                f"{exc.__class__.__name__}: {exc}",
                event="enter_soccer_mode_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                robot_name=self.display_name,
                reason=reason,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            return False
        return True

    def _dispatch_velocity(
        self,
        move: MoveIntent,
        reason: str,
    ) -> None:
        try:
            self._set_velocity(vx=move.vx, vy=move.vy, vyaw=move.vyaw)
        except Exception as exc:
            self._logger.warn(
                f"set_velocity failed for player {self._player_id}: "
                f"{exc.__class__.__name__}: {exc}",
                event="set_velocity_failed",
                team_id=self._config.team_id,
                player_id=self._player_id,
                error_type=exc.__class__.__name__,
                error=str(exc),
                vx=round(move.vx, 3),
                vy=round(move.vy, 3),
                vyaw=round(move.vyaw, 3),
                reason=reason,
            )


class TeamRobotManager:
    """Multi-robot manager: pure routing plus team-level lifecycle.

    This class no longer holds per-player state dictionaries; all per-robot control
    logic lives in :class:`RobotClient`. The manager does only these things:

    Lifecycle**: ``start`` creates and starts clients, and ``close`` closes each client.
    Runtime team control**: ``stop_all`` sends team stop commands without closing hardware.
    Routing**: forward apply/status/get-up/walk-mode calls to clients by ``player_id``.
    Queries**: ``get_client`` and ``robot_name_for_player``.

    BT leaves access hardware through :class:`src.runtime.RobotServices`, not this
    class directly, avoiding cyclic dependencies.
    """

    def __init__(
        self,
        config: SoccerConfig,
        logger: Any,
    ):
        self._config = config
        self._logger = logger
        self._clients: dict[int, RobotClient] = {}

    # Lifecycle: start to close

    def start(self) -> None:
        if self._clients:
            return
        for player_id, robot_name in enumerate(self._config.robot_names, start=1):
            client = RobotClient(
                player_id=player_id,
                robot_name=robot_name,
                config=self._config,
                logger=self._logger,
            )
            self._clients[player_id] = client
            client.start()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # Runtime team control

    def stop_all(self, reason: str = "stop all") -> None:
        """Send stop commands to the whole team at runtime; this is not lifecycle shutdown.

        Used for control-loop exceptions or GameController loss to bring team
        velocity to zero while keeping hardware open for next tick. Use :meth:`close` for lifecycle shutdown.
        """

        now = time.monotonic()
        for client in self._clients.values():
            client.poll_status(now)
            client.apply(RobotCommand.stop(reason))

    # Routing and queries

    def get_client(self, player_id: int) -> RobotClient | None:
        return self._clients.get(player_id)

    def apply_command(self, player_id: int, command: RobotCommand) -> None:
        if isinstance(command.intent, NoopIntent):
            return
        client = self._clients.get(player_id)
        if client is not None:
            client.apply(command)

    def poll_runtime_status(self, player_id: int, now: float) -> RobotRuntimeStatus:
        client = self._clients.get(player_id)
        if client is None:
            return RobotRuntimeStatus()
        return client.poll_status(now)

    def trigger_get_up(self, player_id: int, now: float) -> bool:
        client = self._clients.get(player_id)
        if client is None:
            return False
        return client.trigger_get_up(now)

    def ensure_walk_mode(self, player_id: int, reason: str) -> None:
        client = self._clients.get(player_id)
        if client is not None:
            client.ensure_walk_mode(reason)

    def robot_name_for_player(self, player_id: int) -> str:
        client = self._clients.get(player_id)
        return client.robot_name if client is not None else ""
