"""Kompatibilitäts-Layer für Discord Voice E2EE (DAVE) mit `discord.py 2.7.x`.

Hintergrund:
- Seit `davey 0.2.0` (02.11.2026) müssen nach `process_welcome` / `process_commit` explizit
  `prepare_transition(...)` und `execute_transition(...)` aufgerufen werden (siehe `Rapptz/discord.py`
  PR #10548 / Issue #10549). Ohne diesen Patch zeigt der Bot in Discord zwar den grünen Redekreis,
  verschlüsselt ausgehende Opus-Pakete aber ohne aktiven MLS-Gruppenschlüssel -> kompletter Tonausfall.
- Dieses Modul macht `discord.py 2.7.x` sowohl mit `davey 0.2.0+` als auch mit `davey 0.1.7`
  vollständig kompatibel, ohne dass Pakete manuell um-installiert werden müssen.
"""
from __future__ import annotations

import logging
import struct
from typing import Any

_log = logging.getLogger("modcheck.dave")
_PATCH_APPLIED = False


def _connected_uids(state: Any) -> set[int]:
    uids = getattr(state, "connected_user_ids", None)
    if uids is None:
        uids = set()
        state.connected_user_ids = uids
    return uids


def apply_dave_compat() -> str:
    """Wendet bei Bedarf den DAVE-0.2.0-Kompatibilitätspatch auf `discord.py 2.7.x` an."""
    global _PATCH_APPLIED

    import discord
    import discord.gateway as _gw
    import discord.opus as _opus
    import discord.voice_client as _vc
    import discord.voice_state as _vs

    if not getattr(_vs, "has_dave", False):
        return f"discord.py {discord.__version__} (WARNUNG: davey nicht installiert -> has_dave=False)"

    davey = _vs.davey
    davey_ver = getattr(davey, "__version__", "unbekannt")
    is_davey_v2 = hasattr(davey.DaveSession, "prepare_transition") and hasattr(
        davey.DaveSession, "execute_transition"
    )

    # Wenn eine zukünftige discord.py-Version (>= 2.8) den Patch bereits nativ enthält, nichts überschreiben
    if hasattr(_vs.VoiceConnectionState, "connected_user_ids") or _PATCH_APPLIED:
        return f"discord.py {discord.__version__} + davey {davey_ver} (nativ)"

    _PATCH_APPLIED = True

    # Gemeinsamer Bugfix für 0.1.7 & 0.2.0+: can_encrypt prüft auch dave_downgraded
    def _patched_can_encrypt(self: Any) -> bool:
        return (
            self.dave_protocol_version != 0
            and not getattr(self, "dave_downgraded", False)
            and self.dave_session is not None
            and bool(self.dave_session.ready)
        )

    _vs.VoiceConnectionState.can_encrypt = property(_patched_can_encrypt)  # type: ignore[assignment]

    if not is_davey_v2:
        # davey 0.1.7: nur den set_passthrough_mode(False, 10) Typo in _execute_transition korrigieren
        async def _patched_exec_v1(self: Any, transition_id: int) -> None:
            if transition_id not in self.dave_pending_transitions:
                return
            old_version = self.dave_protocol_version
            self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)
            if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
                self.dave_downgraded = True
            elif transition_id > 0 and self.dave_downgraded:
                self.dave_downgraded = False
                if self.dave_session:
                    self.dave_session.set_passthrough_mode(False, 10)

        _vs.VoiceConnectionState._execute_transition = _patched_exec_v1  # type: ignore[assignment]
        return f"discord.py {discord.__version__} + davey {davey_ver} (DAVE v0.1 aktiv)"

    # ── Vollständiger Rapptz/discord.py PR #10548 Patch für davey >= 0.2.0 ──

    _orig_connect_ws = _vs.VoiceConnectionState._connect_websocket

    async def _patched_connect_websocket(self: Any, resume: bool) -> Any:
        if not resume:
            _connected_uids(self).clear()
        return await _orig_connect_ws(self, resume)

    async def _patched_reinit_dave_session(self: Any) -> None:
        self.dave_pending_transitions.clear()
        if self.dave_protocol_version > 0:
            if not _vs.has_dave:
                raise RuntimeError("davey library needed in order to use E2EE voice")
            session = self.dave_session
            if session is not None:
                session.reinit(self.dave_protocol_version, self.user.id, self.voice_client.channel.id)
            else:
                session = davey.DaveSession(
                    self.dave_protocol_version, self.user.id, self.voice_client.channel.id
                )
                self.dave_session = session

            if getattr(self, "dave_downgraded", False):
                session.set_passthrough_mode(True)

            await self.voice_client.ws.send_binary(
                _gw.DiscordVoiceWebSocket.MLS_KEY_PACKAGE,
                session.get_serialized_key_package(),
            )
        elif self.dave_session:
            self.dave_session.reset()
            self.dave_session.set_passthrough_mode(True)

    async def _patched_execute_transition(self: Any, transition_id: int) -> None:
        if transition_id not in self.dave_pending_transitions:
            return

        # Transition-ID 0 wird von davey bereits in prepare_transition ausgeführt
        if self.dave_session and transition_id != 0:
            try:
                if not self.dave_session.execute_transition(transition_id):
                    self.dave_pending_transitions.pop(transition_id, None)
                    return
            except Exception as exc:
                _log.warning("Failed to execute DAVE transition %s: %s", transition_id, exc)
                self.dave_pending_transitions.pop(transition_id, None)
                return

        old_version = self.dave_protocol_version
        self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)

        if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
            self.dave_downgraded = True
        elif transition_id > 0 and getattr(self, "dave_downgraded", False):
            self.dave_downgraded = False
            if self.dave_session:
                self.dave_session.set_passthrough_mode(False, 10)

    async def _patched_received_message(self: Any, msg: dict[str, Any]) -> None:
        op = msg["op"]
        data = msg["d"]
        self.seq_ack = msg.get("seq", self.seq_ack)
        state = self._connection

        if op == self.READY:
            await self.initial_connection(data)
        elif op == self.HEARTBEAT_ACK:
            if self._keep_alive:
                self._keep_alive.ack()
        elif op == self.RESUMED:
            pass
        elif op == self.SESSION_DESCRIPTION:
            state.mode = data["mode"]
            await self.load_secret_key(data)
            state.dave_protocol_version = data["dave_protocol_version"]
            state.dave_downgraded = data["dave_protocol_version"] == 0
            if data["dave_protocol_version"] > 0:
                await state.reinit_dave_session()
        elif op == self.HELLO:
            interval = data["heartbeat_interval"] / 1000.0
            self._keep_alive = _gw.VoiceKeepAliveHandler(ws=self, interval=min(interval, 5.0))
            self._keep_alive.start()
        elif op == self.CLIENTS_CONNECT:
            if isinstance(data, dict):
                _connected_uids(state).update(int(uid) for uid in data.get("user_ids", ()))
        elif op == self.CLIENT_CONNECT:
            if isinstance(data, dict) and "user_id" in data:
                _connected_uids(state).add(int(data["user_id"]))
        elif op == self.CLIENT_DISCONNECT:
            if isinstance(data, dict) and "user_id" in data:
                _connected_uids(state).discard(int(data["user_id"]))
        elif state.dave_session:
            if op == self.DAVE_PREPARE_TRANSITION:
                state.dave_pending_transitions[data["transition_id"]] = data["protocol_version"]
                try:
                    state.dave_session.prepare_transition(data["transition_id"], data["protocol_version"])
                except Exception as exc:
                    _log.warning("Failed to prepare DAVE transition %s: %s", data["transition_id"], exc)

                if data["protocol_version"] == 0:
                    state.dave_session.set_passthrough_mode(True)

                if data["transition_id"] == 0:
                    await state._execute_transition(data["transition_id"])
                else:
                    await self.send_transition_ready(data["transition_id"])
            elif op == self.DAVE_EXECUTE_TRANSITION:
                await state._execute_transition(data["transition_id"])
            elif op == self.DAVE_PREPARE_EPOCH:
                if data["epoch"] == 1:
                    state.dave_protocol_version = data["protocol_version"]
                    await state.reinit_dave_session()

        await self._hook(self, msg)

    async def _patched_received_binary_message(self: Any, msg: bytes) -> None:
        self.seq_ack = struct.unpack_from(">H", msg, 0)[0]
        op = msg[2]
        state = self._connection

        if state.dave_session is None:
            return

        if op == self.MLS_EXTERNAL_SENDER:
            state.dave_session.set_external_sender(msg[3:])
        elif op == self.MLS_PROPOSALS:
            optype = msg[3]
            op_enum = (
                davey.ProposalsOperationType.append
                if optype == 0
                else davey.ProposalsOperationType.revoke
            )
            uids = _connected_uids(state)
            try:
                roster = [*uids, state.user.id] if uids else None
                result = state.dave_session.process_proposals(op_enum, msg[4:], roster)
            except Exception:
                result = state.dave_session.process_proposals(op_enum, msg[4:], None)
            if isinstance(result, davey.CommitWelcome):
                await self.send_binary(
                    _gw.DiscordVoiceWebSocket.MLS_COMMIT_WELCOME,
                    result.commit + result.welcome if result.welcome else result.commit,
                )
        elif op == self.MLS_ANNOUNCE_COMMIT_TRANSITION:
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            try:
                if not state.dave_session.process_commit(msg[5:]):
                    return
                state.dave_session.prepare_transition(
                    transition_id, state.dave_session.protocol_version
                )
                if transition_id != 0:
                    state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                    await self.send_transition_ready(transition_id)
            except Exception as exc:
                _log.warning("MLS commit error (transition %s): %s", transition_id, exc)
                await state._recover_from_invalid_commit(transition_id)
        elif op == self.MLS_WELCOME:
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            try:
                uids = _connected_uids(state)
                try:
                    roster = [*uids, state.user.id] if uids else None
                    state.dave_session.process_welcome(msg[5:], roster)
                except Exception:
                    state.dave_session.process_welcome(msg[5:], None)
                state.dave_session.prepare_transition(
                    transition_id, state.dave_session.protocol_version
                )
                if transition_id != 0:
                    state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                    await self.send_transition_ready(transition_id)
            except Exception as exc:
                _log.warning("MLS welcome error (transition %s): %s", transition_id, exc)
                await state._recover_from_invalid_commit(transition_id)

    def _patched_get_voice_packet(self: Any, data: bytes) -> bytes | None:
        packet = data
        conn = self._connection
        if conn.dave_protocol_version > 0 and not getattr(conn, "dave_downgraded", False):
            sess = conn.dave_session
            if sess is None or not getattr(sess, "ready", False) or not getattr(sess, "can_encrypt", True):
                return None
            packet = sess.encrypt_opus(data)

        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into(">H", header, 2, self.sequence)
        struct.pack_into(">I", header, 4, self.timestamp)
        struct.pack_into(">I", header, 8, self.ssrc)

        encrypt_packet = getattr(self, "_encrypt_" + self.mode)
        return encrypt_packet(header, packet)

    def _patched_send_audio_packet(self: Any, data: bytes, *, encode: bool = True) -> None:
        self.checked_add("sequence", 1, 65535)
        if encode:
            encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
        else:
            encoded_data = data
        packet = self._get_voice_packet(encoded_data)
        if packet is not None:
            try:
                self._connection.send_packet(packet)
            except OSError:
                pass
        self.checked_add("timestamp", _opus.Encoder.SAMPLES_PER_FRAME, 4294967295)

    _vs.VoiceConnectionState._connect_websocket = _patched_connect_websocket  # type: ignore[assignment]
    _vs.VoiceConnectionState.reinit_dave_session = _patched_reinit_dave_session  # type: ignore[assignment]
    _vs.VoiceConnectionState._execute_transition = _patched_execute_transition  # type: ignore[assignment]
    _gw.DiscordVoiceWebSocket.received_message = _patched_received_message  # type: ignore[assignment]
    _gw.DiscordVoiceWebSocket.received_binary_message = _patched_received_binary_message  # type: ignore[assignment]
    _vc.VoiceClient._get_voice_packet = _patched_get_voice_packet  # type: ignore[assignment]
    _vc.VoiceClient.send_audio_packet = _patched_send_audio_packet  # type: ignore[assignment]

    return f"discord.py {discord.__version__} + davey {davey_ver} (PR #10548 DAVE-v0.2 Patch aktiv)"
