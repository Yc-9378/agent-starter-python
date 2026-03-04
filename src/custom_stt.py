import asyncio
import contextlib
import gzip
import json
import logging
import os
import time
import uuid
from typing import Any

import websockets
from livekit.agents import stt
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import audio
from websockets import ConnectionClosed

logger = logging.getLogger("agent")

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message Type
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Message Type Specific Flags
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_WITH_SEQUENCE = 0b0011

# Message Serialization
NO_SERIALIZATION = 0b0000
JSON = 0b0001

# Message Compression
NO_COMPRESSION = 0b0000
GZIP = 0b0001


def get_default_stt_config() -> Any:
    """Get default STT config from config.py when available."""
    try:
        from config import config

        return config.stt
    except ImportError:
        class DefaultSTTConfig:
            ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
            model = "bigmodel"
            sample_rate = 16000
            num_channels = 1
            language = "zh-CN"
            format = "pcm"
            bits = 16
            codec = "raw"
            seg_duration = 100
            streaming = True

        return DefaultSTTConfig()


def generate_header(
    message_type: int = FULL_CLIENT_REQUEST,
    message_type_specific_flags: int = NO_SEQUENCE,
    serial_method: int = JSON,
    compression_type: int = GZIP,
    reserved_data: int = 0x00,
) -> bytearray:
    """Generate huoshan ASR protocol header."""
    header = bytearray()
    header_size = DEFAULT_HEADER_SIZE
    header.append((PROTOCOL_VERSION << 4) | header_size)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    return header


def generate_before_payload(sequence: int) -> bytearray:
    before_payload = bytearray()
    before_payload.extend(sequence.to_bytes(4, "big", signed=True))
    return before_payload


class CustomSTT(stt.STT):
    def __init__(
        self,
        ws_url: str | None = None,
        model: str | None = None,
        sample_rate: int | None = None,
        num_channels: int | None = None,
        language: str | None = None,
        api_key: str | None = None,
        app_id: str | None = None,
        audio_format: str | None = None,
        bits: int | None = None,
        codec: str | None = None,
        seg_duration: int | None = None,
        streaming: bool | None = None,
        **kwargs: Any,
    ) -> None:
        default_config = get_default_stt_config()

        legacy_format = kwargs.pop("format", None)
        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs.keys())}")

        self.ws_url = ws_url or default_config.ws_url
        self._model_name = model or default_config.model
        self.sample_rate = sample_rate or default_config.sample_rate
        self.num_channels = num_channels or default_config.num_channels
        self.language = language or default_config.language
        self.api_key = api_key or os.getenv("HUOSHAN_ACCESS_TOKEN")
        self.app_id = app_id or os.getenv("HUOSHAN_APPID")
        self.format = audio_format or legacy_format or default_config.format
        self.bits = bits or default_config.bits
        self.codec = codec or default_config.codec
        self.seg_duration = seg_duration or default_config.seg_duration
        self.streaming = streaming if streaming is not None else default_config.streaming

        if not self.api_key:
            raise ValueError(
                "HUOSHAN_ACCESS_TOKEN is required, either as argument or environment variable"
            )
        if not self.app_id:
            raise ValueError(
                "HUOSHAN_APPID is required, either as argument or environment variable"
            )

        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                offline_recognize=False,
            )
        )

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def provider(self) -> str:
        return "huoshan"

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options=DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        stream_language = self.language if language is NOT_GIVEN else language
        return CustomSTTStream(stt_instance=self, language=stream_language, conn_options=conn_options)

    async def _recognize_impl(
        self,
        buffer: audio.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options,
    ) -> stt.SpeechEvent:
        raise APIConnectionError(
            "CustomSTT only supports streaming mode. Use stt.stream() in AgentSession."
        )


class CustomSTTStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt_instance: CustomSTT,
        language: str,
        conn_options,
    ) -> None:
        super().__init__(
            stt=stt_instance,
            conn_options=conn_options,
            sample_rate=stt_instance.sample_rate,
        )
        self._stt_impl = stt_instance
        self._language = language
        self._ws = None
        self._connected = False
        self._reqid = ""
        self._seq = 2
        self._connect_backoff_s = 0.0
        self._next_connect_at = 0.0
        self._audio_packets_sent = 0
        self._responses_received = 0
        self._frames_received = 0
        bytes_per_sample = max(1, self._stt_impl.bits // 8)
        self._segment_size_bytes = max(
            1,
            int(
                self._stt_impl.sample_rate
                * self._stt_impl.num_channels
                * bytes_per_sample
                * self._stt_impl.seg_duration
                / 1000
            ),
        )
        self._pending_audio = bytearray()
        self._send_interval_s = max(0.02, self._stt_impl.seg_duration / 1000)
        self._send_queue: asyncio.Queue[tuple[bytes, bool] | None] = asyncio.Queue()
        self._latest_transcript_text = ""
        self._final_event = asyncio.Event()
        self._speech_started = False
        self._sent_last_audio_packet = False
        self._last_seen_text = ""
        self._same_text_hits = 0
        self._last_final_text = ""
        self._send_started_logged = False

    def _ensure_runtime_state(self) -> None:
        if not hasattr(self, "_send_queue"):
            self._send_queue = asyncio.Queue()
        if not hasattr(self, "_send_interval_s"):
            self._send_interval_s = 0.2
        if not hasattr(self, "_connected"):
            self._connected = False
        if not hasattr(self, "_ws"):
            self._ws = None
        if not hasattr(self, "_latest_transcript_text"):
            self._latest_transcript_text = ""
        if not hasattr(self, "_final_event"):
            self._final_event = asyncio.Event()
        if not hasattr(self, "_speech_started"):
            self._speech_started = False
        if not hasattr(self, "_sent_last_audio_packet"):
            self._sent_last_audio_packet = False
        if not hasattr(self, "_last_seen_text"):
            self._last_seen_text = ""
        if not hasattr(self, "_same_text_hits"):
            self._same_text_hits = 0
        if not hasattr(self, "_last_final_text"):
            self._last_final_text = ""
        if not hasattr(self, "_send_started_logged"):
            self._send_started_logged = False

    def _construct_request(self) -> dict[str, Any]:
        return {
            "user": {"uid": "livekit-agent"},
            "audio": {
                "format": self._stt_impl.format,
                "sample_rate": self._stt_impl.sample_rate,
                "bits": self._stt_impl.bits,
                "channel": self._stt_impl.num_channels,
                "codec": self._stt_impl.codec,
            },
            "request": {
                "model_name": self._stt_impl.model,
                "enable_punc": True,
                "result_type": "single",
                "enable_ddc": False,
                "end_window_size": 1000,
                "force_to_speech_time": 500,
            },
        }

    def _build_init_frame(self) -> bytes:
        payload_bytes = gzip.compress(json.dumps(self._construct_request()).encode("utf-8"))
        frame = bytearray(generate_header(message_type_specific_flags=POS_SEQUENCE))
        frame.extend(generate_before_payload(sequence=1))
        frame.extend(len(payload_bytes).to_bytes(4, "big", signed=False))
        frame.extend(payload_bytes)
        return bytes(frame)

    def _build_audio_frame(self, chunk: bytes, *, last: bool) -> bytes:
        payload_bytes = gzip.compress(chunk)
        flags = NEG_WITH_SEQUENCE if last else POS_SEQUENCE

        seq = self._seq
        self._seq += 1
        if last:
            seq = -seq

        frame = bytearray(
            generate_header(
                message_type=AUDIO_ONLY_REQUEST,
                message_type_specific_flags=flags,
            )
        )
        frame.extend(generate_before_payload(sequence=seq))
        frame.extend(len(payload_bytes).to_bytes(4, "big", signed=False))
        frame.extend(payload_bytes)
        return bytes(frame)

    def _build_ws_headers(self) -> dict[str, str]:
        return {
            "X-Api-Resource-Id": "volc.bigasr.sauc.duration",
            "X-Api-Access-Key": self._stt_impl.api_key,
            "X-Api-App-Key": self._stt_impl.app_id,
            "X-Api-Request-Id": self._reqid,
        }

    def _parse_response(self, res: bytes) -> dict[str, Any] | None:
        if not res:
            return None

        header_size = res[0] & 0x0F
        message_type = res[1] >> 4
        message_type_specific_flags = res[1] & 0x0F
        serialization_method = res[2] >> 4
        message_compression = res[2] & 0x0F

        payload = res[header_size * 4 :]
        result: dict[str, Any] = {
            "is_last_package": False,
            "message_type": message_type,
        }

        if message_type_specific_flags & 0x01:
            seq = int.from_bytes(payload[:4], "big", signed=True)
            result["payload_sequence"] = seq
            payload = payload[4:]

        if message_type_specific_flags & 0x02:
            result["is_last_package"] = True

        payload_msg = None
        payload_size = 0

        if message_type == FULL_SERVER_RESPONSE:
            payload_size = int.from_bytes(payload[:4], "big", signed=False)
            payload_msg = payload[4:]
        elif message_type == SERVER_ACK:
            seq = int.from_bytes(payload[:4], "big", signed=True)
            result["seq"] = seq
            if len(payload) >= 8:
                payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                payload_msg = payload[8:]
        elif message_type == SERVER_ERROR_RESPONSE:
            code = int.from_bytes(payload[:4], "big", signed=False)
            result["code"] = code
            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
            payload_msg = payload[8:]

        if payload_msg is None:
            return result

        if message_compression == GZIP:
            payload_msg = gzip.decompress(payload_msg)

        if serialization_method == JSON:
            payload_msg = json.loads(payload_msg.decode("utf-8"))
        elif serialization_method != NO_SERIALIZATION:
            payload_msg = payload_msg.decode("utf-8")

        result["payload_msg"] = payload_msg
        result["payload_size"] = payload_size
        return result

    def _validate_init_response(self, response: bytes) -> None:
        parsed = self._parse_response(response)
        if not parsed:
            raise RuntimeError("Huoshan ASR init failed: empty response")

        code = parsed.get("code")
        if code:
            payload = parsed.get("payload_msg")
            raise RuntimeError(f"Huoshan ASR init failed: code={code}, payload={payload}")

    async def _close_websocket(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        self._ws = None
        self._connected = False

    def _reset_sequence_for_new_session(self) -> None:
        self._seq = 2

    def _mark_connect_failure(self) -> None:
        self._connect_backoff_s = 0.5 if self._connect_backoff_s == 0 else min(8.0, self._connect_backoff_s * 2)
        self._next_connect_at = time.monotonic() + self._connect_backoff_s

    def _mark_connect_success(self) -> None:
        self._connect_backoff_s = 0.0
        self._next_connect_at = 0.0

    async def _connect_websocket(self) -> bool:
        if self._connected and self._ws is not None:
            return True

        if self._next_connect_at > time.monotonic():
            await asyncio.sleep(self._next_connect_at - time.monotonic())

        self._reqid = str(uuid.uuid4())
        self._reset_sequence_for_new_session()
        logger.debug(
            "CustomSTT connecting to huoshan websocket, reqid=%s, url=%s",
            self._reqid,
            self._stt_impl.ws_url,
        )
        try:
            self._ws = await websockets.connect(
                self._stt_impl.ws_url,
                additional_headers=self._build_ws_headers(),
                max_size=100_000_000,
            )
            await self._ws.send(self._build_init_frame())
            init_response = await self._ws.recv()
            self._validate_init_response(init_response)

            self._connected = True
            self._mark_connect_success()
            logger.debug("CustomSTT websocket connected, reqid=%s", self._reqid)
            return True
        except Exception:
            self._mark_connect_failure()
            logger.exception(
                "CustomSTT websocket connect/init failed, reqid=%s", self._reqid
            )
            await self._close_websocket()
            return False

    def _extract_transcript(self, payload_msg: Any) -> tuple[str, bool]:
        if not isinstance(payload_msg, dict):
            return "", False

        result = payload_msg.get("result")
        if isinstance(result, dict):
            text = str(result.get("text") or "").strip()
            definite = bool(result.get("definite", False))
            if not definite and isinstance(result.get("utterances"), list):
                utterances = result["utterances"]
                if utterances and isinstance(utterances[-1], dict):
                    definite = bool(utterances[-1].get("definite", False))
            return text, definite

        text = str(payload_msg.get("text") or "").strip()
        definite = bool(payload_msg.get("definite", payload_msg.get("is_final", False)))
        return text, definite

    async def _emit_transcript_event(self, payload_msg: Any) -> None:
        text, is_final = self._extract_transcript(payload_msg)
        if not text:
            return

        # Ignore late packets after we have already finalized this speech turn.
        if self._final_event.is_set():
            return

        if text == self._last_seen_text:
            self._same_text_hits += 1
        else:
            self._last_seen_text = text
            self._same_text_hits = 1

        if not is_final and text == self._latest_transcript_text:
            return

        # Primary finalization should come from payload text state, not from local last packet.
        # Rules:
        # 1) Server explicit final (definite/is_final) -> final
        # 2) Sentence punctuation -> final
        # 3) Same transcript repeated >=2 times -> treat as stabilized final
        if not is_final:
            terminal_punctuation = ("\u3002", "\uff01", "\uff1f", "!", "?")
            is_final = (
                text.endswith(terminal_punctuation)
                or (self._same_text_hits >= 2 and len(text) >= 2)
            )

        if is_final and text == self._last_final_text:
            logger.debug(
                "CustomSTT skip duplicate final transcript, reqid=%s, text=%s",
                self._reqid,
                text,
            )
            return

        event_type = (
            stt.SpeechEventType.FINAL_TRANSCRIPT
            if is_final
            else stt.SpeechEventType.INTERIM_TRANSCRIPT
        )
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=event_type,
                request_id=self._reqid,
                alternatives=[
                    stt.SpeechData(
                        language=self._language,
                        text=text,
                        confidence=1.0,
                    )
                ],
            )
        )
        self._latest_transcript_text = text
        if is_final:
            self._last_final_text = text
            self._final_event.set()
            if self._speech_started:
                # Ensure downstream turn handling can continue even when upstream flush is delayed.
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                )
                self._speech_started = False
                logger.debug(
                    "CustomSTT emitted END_OF_SPEECH from final transcript, reqid=%s",
                    self._reqid,
                )
        logger.debug(
            "CustomSTT transcript received, reqid=%s, final=%s, text=%s",
            self._reqid,
            is_final,
            text[:120],
        )

    def _emit_fallback_final_if_needed(self) -> None:
        if self._final_event.is_set():
            return

        text = self._latest_transcript_text.strip()
        if not text:
            return

        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                request_id=self._reqid,
                alternatives=[
                    stt.SpeechData(
                        language=self._language,
                        text=text,
                        confidence=1.0,
                    )
                ],
            )
        )
        self._final_event.set()
        logger.debug(
            "CustomSTT emitted fallback FINAL_TRANSCRIPT, reqid=%s, text=%s",
            self._reqid,
            text[:120],
        )

    async def _send_audio_data(self, chunk: bytes, *, last: bool) -> None:
        self._ensure_runtime_state()
        if not chunk and not last:
            return

        await self._send_queue.put((chunk, last))

    async def _sender_loop(self) -> None:
        self._ensure_runtime_state()
        while True:
            item = await self._send_queue.get()
            if item is None:
                self._send_queue.task_done()
                break

            chunk, last = item
            try:
                if not self._connected:
                    ok = await self._connect_websocket()
                    if not ok:
                        continue

                if self._ws is None:
                    continue

                frame = self._build_audio_frame(chunk, last=last)
                self._audio_packets_sent += 1
                if last:
                    self._sent_last_audio_packet = True
                if not self._send_started_logged:
                    logger.info(
                        "CustomSTT started sending audio, reqid=%s, seg_duration_ms=%s, sample_rate=%s, channels=%s, bits=%s",
                        self._reqid,
                        self._stt_impl.seg_duration,
                        self._stt_impl.sample_rate,
                        self._stt_impl.num_channels,
                        self._stt_impl.bits,
                    )
                    self._send_started_logged = True
                await self._ws.send(frame)
                if not last:
                    await asyncio.sleep(self._send_interval_s)
            except ConnectionClosed:
                logger.warning(
                    "CustomSTT websocket closed while sending audio, reqid=%s", self._reqid
                )
                await self._close_websocket()
                self._mark_connect_failure()
            except Exception as e:
                logger.exception("CustomSTT send failed, reqid=%s", self._reqid)
                self._emit_error(e, recoverable=True)
                await self._close_websocket()
                self._mark_connect_failure()
            finally:
                self._send_queue.task_done()

    async def _receiver_loop(self) -> None:
        self._ensure_runtime_state()
        while True:
            try:
                if not self._connected or self._ws is None:
                    await asyncio.sleep(0.05)
                    continue

                response = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
                self._responses_received += 1
                parsed = self._parse_response(response)
                if parsed and "payload_msg" in parsed:
                    await self._emit_transcript_event(parsed["payload_msg"])
                else:
                    logger.debug(
                        "CustomSTT parsed response without payload_msg, reqid=%s, parsed=%s",
                        self._reqid,
                        parsed,
                    )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except ConnectionClosed:
                logger.warning("CustomSTT websocket closed while receiving, reqid=%s", self._reqid)
                await self._close_websocket()
                self._mark_connect_failure()
            except Exception as e:
                logger.exception("CustomSTT receive failed, reqid=%s", self._reqid)
                self._emit_error(e, recoverable=True)
                await self._close_websocket()
                self._mark_connect_failure()

    async def _drain_send_queue(self) -> None:
        await self._send_queue.join()

    async def _shutdown_worker_tasks(
        self, sender_task: asyncio.Task[None], receiver_task: asyncio.Task[None]
    ) -> None:
        self._ensure_runtime_state()
        await self._send_queue.put(None)
        await sender_task
        receiver_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver_task

    async def _enqueue_audio_chunks(self, *, last: bool) -> None:
        if not self._pending_audio and last:
            await self._send_audio_data(b"", last=True)
            return

        if not last:
            while len(self._pending_audio) >= self._segment_size_bytes:
                chunk = bytes(self._pending_audio[: self._segment_size_bytes])
                del self._pending_audio[: self._segment_size_bytes]
                await self._send_audio_data(chunk, last=False)
            return

        while len(self._pending_audio) > self._segment_size_bytes:
            chunk = bytes(self._pending_audio[: self._segment_size_bytes])
            del self._pending_audio[: self._segment_size_bytes]
            await self._send_audio_data(chunk, last=False)

        tail = bytes(self._pending_audio)
        self._pending_audio.clear()
        await self._send_audio_data(tail, last=True)

    async def _run(self) -> None:
        sender_task = asyncio.create_task(self._sender_loop(), name="CustomSTT.sender")
        receiver_task = asyncio.create_task(
            self._receiver_loop(), name="CustomSTT.receiver"
        )
        try:
            connected = await self._connect_websocket()
            if not connected:
                return

            self._speech_started = False

            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    logger.debug(
                        "CustomSTT received flush sentinel, reqid=%s, pending_bytes=%s",
                        self._reqid,
                        len(self._pending_audio),
                    )
                    if self._speech_started or self._pending_audio:
                        await self._enqueue_audio_chunks(last=True)
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._final_event.wait(), timeout=0.6)
                        self._emit_fallback_final_if_needed()
                        if self._speech_started:
                            self._event_ch.send_nowait(
                                stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                            )
                            self._speech_started = False
                    continue

                self._frames_received += 1
                if self._frames_received == 1 or self._frames_received % 50 == 0:
                    logger.debug(
                        "CustomSTT received audio frame, reqid=%s, frames=%s, frame_bytes=%s",
                        self._reqid,
                        self._frames_received,
                        len(item.data),
                    )
                if not self._speech_started:
                    self._speech_started = True
                    self._final_event.clear()
                    self._latest_transcript_text = ""
                    self._sent_last_audio_packet = False
                    self._last_seen_text = ""
                    self._same_text_hits = 0
                    self._last_final_text = ""
                    self._send_started_logged = False
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                    )

                self._pending_audio.extend(bytes(item.data))
                await self._enqueue_audio_chunks(last=False)

            if self._speech_started or self._pending_audio:
                await self._enqueue_audio_chunks(last=True)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._final_event.wait(), timeout=0.6)
                self._emit_fallback_final_if_needed()
                if self._speech_started:
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                    )
                    self._speech_started = False
            await self._drain_send_queue()

        except Exception as e:
            self._emit_error(e, recoverable=True)
        finally:
            await self._shutdown_worker_tasks(sender_task, receiver_task)
            await self._close_websocket()
