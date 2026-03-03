import asyncio
import gzip
import json
import time
import traceback
import uuid
import wave
from io import BytesIO

import websockets
from get_logger import huoshan_logger
from tools.tool import generate_empty_audio_bytes

from config import HUOSHAN_ACCESS_TOKEN, HUOSHAN_APPID

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message Type:
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Message Type Specific Flags
NO_SEQUENCE = 0b0000  # no check sequence
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_WITH_SEQUENCE = 0b0011
NEG_SEQUENCE_1 = 0b0011

# Message Serialization
NO_SERIALIZATION = 0b0000
JSON = 0b0001

# Message Compression
NO_COMPRESSION = 0b0000
GZIP = 0b0001


def generate_header(
    message_type=FULL_CLIENT_REQUEST,
    message_type_specific_flags=NO_SEQUENCE,
    serial_method=JSON,
    compression_type=GZIP,
    reserved_data=0x00,
):
    """
    protocol_version(4 bits), header_size(4 bits),
    message_type(4 bits), message_type_specific_flags(4 bits)
    serialization_method(4 bits) message_compression(4 bits)
    reserved （8bits) 保留字段
    """
    header = bytearray()
    header_size = 1
    header.append((PROTOCOL_VERSION << 4) | header_size)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    return header


def generate_before_payload(sequence: int):
    before_payload = bytearray()
    before_payload.extend(sequence.to_bytes(4, "big", signed=True))  # sequence
    return before_payload


def parse_response(res):
    """
    protocol_version(4 bits), header_size(4 bits),
    message_type(4 bits), message_type_specific_flags(4 bits)
    serialization_method(4 bits) message_compression(4 bits)
    reserved （8bits) 保留字段
    header_extensions 扩展头(大小等于 8 * 4 * (header_size - 1) )
    payload 类似与http 请求体
    """
    protocol_version = res[0] >> 4
    header_size = res[0] & 0x0F
    message_type = res[1] >> 4
    message_type_specific_flags = res[1] & 0x0F
    serialization_method = res[2] >> 4
    message_compression = res[2] & 0x0F
    reserved = res[3]
    header_extensions = res[4 : header_size * 4]
    payload = res[header_size * 4 :]
    result = {
        "is_last_package": False,
    }
    payload_msg = None
    payload_size = 0
    if message_type_specific_flags & 0x01:
        # receive frame with sequence
        seq = int.from_bytes(payload[:4], "big", signed=True)
        result["payload_sequence"] = seq
        payload = payload[4:]

    if message_type_specific_flags & 0x02:
        # receive last package
        result["is_last_package"] = True

    if message_type == FULL_SERVER_RESPONSE:
        payload_size = int.from_bytes(payload[:4], "big", signed=True)
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
        payload_msg = json.loads(str(payload_msg, "utf-8"))
    elif serialization_method != NO_SERIALIZATION:
        payload_msg = str(payload_msg, "utf-8")
    result["payload_msg"] = payload_msg
    result["payload_size"] = payload_size
    return result


def read_wav_info(data: bytes = None) -> (int, int, int, int, bytes):
    with BytesIO(data) as _f:
        wave_fp = wave.open(_f, "rb")
        nchannels, sampwidth, framerate, nframes = wave_fp.getparams()[:4]
        wave_bytes = wave_fp.readframes(nframes)
    return nchannels, sampwidth, framerate, nframes, wave_bytes


class AsrWsClient:
    def __init__(self, **kwargs):
        """
        :param config: config
        """
        self.success_code = 1000  # success code, default is 1000
        self.seg_duration = int(kwargs.get("seg_duration", 100))
        self.ws_url = kwargs.get(
            "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        )
        self.uid = kwargs.get("uid", "test")
        self.format = kwargs.get("format", "wav")
        self.rate = kwargs.get("rate", 16000)
        self.bits = kwargs.get("bits", 16)
        self.channel = kwargs.get("channel", 1)
        self.codec = kwargs.get("codec", "raw")
        self.auth_method = kwargs.get("auth_method", "none")
        self.hot_words = kwargs.get("hot_words")
        self.streaming = kwargs.get("streaming", True)
        self.mp3_seg_size = kwargs.get("mp3_seg_size", 1000)
        self.req_event = 1
        self.reqid = str(uuid.uuid4())
        self.seq = 1
        self.ws = None
        self.request_params = self.construct_request(self.reqid)
        self.payload_bytes = str.encode(json.dumps(self.request_params))
        self.payload_bytes = gzip.compress(self.payload_bytes)
        self.full_client_request = bytearray(
            generate_header(message_type_specific_flags=POS_SEQUENCE)
        )
        self.full_client_request.extend(generate_before_payload(sequence=self.seq))
        self.full_client_request.extend(
            (len(self.payload_bytes)).to_bytes(4, "big")
        )  # payload size(4 bytes)
        self.full_client_request.extend(self.payload_bytes)  # payload
        self.header = {}
        self.header["X-Api-Resource-Id"] = "volc.bigasr.sauc.duration"
        self.header["X-Api-Access-Key"] = HUOSHAN_ACCESS_TOKEN
        self.header["X-Api-App-Key"] = HUOSHAN_APPID
        self.header["X-Api-Request-Id"] = self.reqid
        self.send_asr_time = None
        self.send_asr_task = asyncio.create_task(self.send_asr())
        self.send_asr_lock = asyncio.Lock()

    # 每0.2秒发送ping
    async def send_asr(self):
        try:
            empty_audio_bytes = generate_empty_audio_bytes(500)  # 500ms的空音频
            while True:
                if self.ws:
                    if (
                        self.send_asr_time
                        and (time.time() - self.send_asr_time) * 1000 >= 500
                    ):
                        self.send_asr_time = time.time()
                        await self.segment_data_processor(empty_audio_bytes)
                # await self.ws.ping()
                await asyncio.sleep(0.5)
        except Exception:
            huoshan_logger.info(f"发送空音频失败，{traceback.format_exc()}")

    async def close_ws(self):
        if self.ws:
            await self.ws.close()
        self.ws = None
        if self.send_asr_task:
            self.send_asr_task.cancel()
            try:
                await self.send_asr_task
            except asyncio.CancelledError:
                pass
        huoshan_logger.info("火山ASR资源已关闭")

    def construct_request(self, reqid, data=None):
        req = {
            "user": {
                "uid": self.uid,
            },
            "audio": {
                "format": self.format,
                "sample_rate": self.rate,
                "bits": self.bits,
                "channel": self.channel,
                "codec": self.codec,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,  # 是否启用标点
                "result_type": "single",  # 默认为"full",全量返回。设置为"single"则为增量结果返回，即不返回之前分句的结果。
                "enable_ddc": False,  # 是否启用语义顺滑，
                # "vad_segment_duration": 3000,
                "end_window_size": 1000,  # 强制判停时间，静音时长超过该值，会直接判停
                "force_to_speech_time": 500,  # 强制语音时间，音频时长超过该值之后，才会判停，根据静音时长输出definite，需配合end_window_size使用。
            },
        }
        return req

    @staticmethod
    def slice_data(data: bytes, chunk_size: int) -> (list, bool):
        data_len = len(data)
        offset = 0
        while offset + chunk_size < data_len:
            yield data[offset : offset + chunk_size], False
            offset += chunk_size
        else:
            yield data[offset:data_len], True

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url, additional_headers=self.header, max_size=1000000000
        )
        await self.ws.send(self.full_client_request)
        huoshan_logger.info("火山ASR WS链接成功")

    async def segment_data_processor(self, chunk, last=False):
        try:
            async with self.send_asr_lock:
                if self.ws is None:
                    await self.connect()
                self.send_asr_time = time.time()
                self.seq += 1
                if last:
                    self.seq = -self.seq
                start = time.time()
                payload_bytes = gzip.compress(chunk)
                audio_only_request = bytearray(
                    generate_header(
                        message_type=AUDIO_ONLY_REQUEST,
                        message_type_specific_flags=POS_SEQUENCE,
                    )
                )
                if last:
                    audio_only_request = bytearray(
                        generate_header(
                            message_type=AUDIO_ONLY_REQUEST,
                            message_type_specific_flags=NEG_WITH_SEQUENCE,
                        )
                    )
                audio_only_request.extend(generate_before_payload(sequence=self.seq))
                audio_only_request.extend(
                    (len(payload_bytes)).to_bytes(4, "big")
                )  # payload size(4 bytes)
                audio_only_request.extend(payload_bytes)  # payload
                await self.ws.send(audio_only_request)
                res = await self.ws.recv()
                result = parse_response(res)
                if self.streaming:
                    sleep_time = max(
                        0, (self.seg_duration / 1000.0 - (time.time() - start))
                    )
                    await asyncio.sleep(sleep_time)
                if "payload_msg" in result:
                    return result["payload_msg"]
                return None
        except websockets.exceptions.ConnectionClosedOK:
            # 客户端/服务端正常关闭（1000），不应当报错重连
            huoshan_logger.info("火山ASR WS已正常关闭，跳过重连")
            self.ws = None
            return None
        except Exception as e:
            huoshan_logger.error(
                f"火山ASR WS链接断开重连，错误信息：{traceback.format_exc()}"
            )
            if self.ws:
                await self.ws.close(code=1011, reason=str(e))
            self.seq = 1
            await self.connect()

    async def execute(self, audio_data):
        if self.format == "mp3":
            segment_size = self.mp3_seg_size
        elif self.format == "wav":
            nchannels, sampwidth, framerate, nframes, wav_len = read_wav_info(
                audio_data
            )
            size_per_sec = nchannels * sampwidth * framerate
            segment_size = int(size_per_sec * self.seg_duration / 1000)
        elif self.format == "pcm":
            segment_size = int(self.rate * 2 * self.channel * self.seg_duration / 500)
        else:
            raise Exception("Unsupported format")
        results = []
        for chunk, last in self.slice_data(audio_data, segment_size):
            result = await self.segment_data_processor(chunk, last)
            if result:
                results.append(result)
        return results

    async def close(self):
        if self.ws:
            await self.ws.close()
