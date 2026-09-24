"""容器解析与无损抽取的往返测试。

核心断言不是「函数没报错」，而是**抽取出来的音轨字节必须与源文件里的音轨字节
逐字节相同**——这才证明块偏移的平移是对的（最容易错的地方）。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app import container  # noqa: E402
from tests import synth  # noqa: E402


class TempDirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="shh11-test-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path


class TestMp4(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # 每块内容不同，且长度不同 —— 交错排列下偏移若算错必然对不上
        self.audio = [b"AUDIO-CHUNK-%02d-" % i + bytes([0x41 + i]) * (100 + i * 7)
                      for i in range(4)]
        self.video = [b"VIDEO-CHUNK-%02d-" % i + bytes([0x61 + i]) * (500 + i * 13)
                      for i in range(4)]
        data, _, _ = synth.build_mp4(self.audio, self.video)
        self.path = self.write("sample.mp4", data)

    def test_probe_lists_both_tracks(self):
        info = container.probe(self.path)
        self.assertEqual(info["family"], "iso-bmff")
        self.assertEqual(len(info["tracks"]), 2)
        kinds = sorted(track["kind"] for track in info["tracks"])
        self.assertEqual(kinds, ["audio", "video"])
        self.assertEqual(len(info["audio_tracks"]), 1)

    def test_audio_track_metadata(self):
        info = container.probe(self.path)
        audio = info["audio_tracks"][0]
        self.assertEqual(audio["track_id"], 1)
        self.assertEqual(audio["codec"], "mp4a")
        self.assertEqual(audio["codec_name"], "AAC")
        self.assertEqual(audio["channels"], 2)
        self.assertEqual(audio["sample_rate"], 44100)
        self.assertTrue(audio["extractable"])

    def test_video_track_not_marked_extractable(self):
        info = container.probe(self.path)
        video = next(t for t in info["tracks"] if t["kind"] == "video")
        self.assertFalse(video["extractable"])

    def test_extract_is_byte_identical(self):
        """抽取出的音频负载必须与源文件里的音频块逐字节相同。"""
        out = os.path.join(self.tmp, "out.m4a")
        result = container.extract(self.path, 1, out)
        self.assertTrue(result["stream_copy"])
        self.assertTrue(os.path.exists(out))

        original = b"".join(self.audio)

        # 用同一个解析器读回输出，再按它自己的 stco 把音频负载读出来
        info = container.parse_mp4(out)
        tracks = info["tracks"]
        self.assertEqual(len(tracks), 1, "抽取后应当只剩一条轨道")
        self.assertEqual(tracks[0]["kind"], "audio")
        self.assertEqual(tracks[0]["codec"], "mp4a", "编码信息必须在重建中保住")

        extracted = bytearray()
        index = tracks[0]["index"]
        with open(out, "rb") as fh:
            for offset, size in zip(index["chunk_offsets"], tracks[0]["chunk_sizes"]):
                fh.seek(offset)
                extracted.extend(fh.read(size))
        self.assertEqual(bytes(extracted), original)

    def test_extract_output_is_smaller_than_source(self):
        out = os.path.join(self.tmp, "out.m4a")
        container.extract(self.path, 1, out)
        source_size = os.path.getsize(self.path)
        out_size = os.path.getsize(out)
        video_bytes = sum(len(chunk) for chunk in self.video)
        self.assertLess(out_size, source_size)
        # 大致应等于「音频字节 + 容器开销」，绝不该把视频也搬过来
        self.assertLess(out_size, source_size - video_bytes + 4096)

    def test_extract_video_track_is_refused(self):
        out = os.path.join(self.tmp, "bad.m4a")
        with self.assertRaises(container.ContainerError):
            container.extract(self.path, 2, out)

    def test_unknown_track_is_refused(self):
        out = os.path.join(self.tmp, "bad2.m4a")
        with self.assertRaises(container.ContainerError):
            container.extract(self.path, 99, out)

    def test_suggested_output_extension(self):
        info = container.probe(self.path)
        name = container.suggested_output(self.path, info["audio_tracks"][0], self.tmp)
        self.assertTrue(name.endswith(".m4a"), name)

    def test_truncated_file_raises(self):
        data = open(self.path, "rb").read()
        broken = self.write("broken.mp4", data[:len(data) // 3])
        with self.assertRaises(container.ContainerError):
            container.parse_mp4(broken)

    def test_non_media_file_raises_unsupported(self):
        path = self.write("readme.txt", b"this is not a media file at all")
        with self.assertRaises(container.UnsupportedFormat):
            container.probe(path)

    def test_progress_is_reported(self):
        calls = []
        out = os.path.join(self.tmp, "progress.m4a")
        container.extract(self.path, 1, out, progress=lambda done, total: calls.append((done, total)))
        self.assertTrue(calls)
        self.assertEqual(calls[-1][0], calls[-1][1], "最后一次进度应当是 100%")


class TestMp4MultiSampleChunks(TempDirMixin, unittest.TestCase):
    """每个块含多个样本时的路径 —— 走的是 stsc/stsz 的样本游标累加逻辑。"""

    def setUp(self):
        super().setUp()
        self.audio = [bytes([0x10 + i]) * (101 + i * 17) for i in range(3)]
        self.video = [bytes([0x50 + i]) * (400 + i * 29) for i in range(3)]
        data, _, _ = synth.build_mp4(self.audio, self.video, samples_per_chunk=4)
        self.path = self.write("multi.mp4", data)

    def test_chunk_sizes_reconstruct_exactly(self):
        info = container.parse_mp4(self.path)
        audio = next(t for t in info["tracks"] if t["kind"] == "audio")
        self.assertEqual(audio["chunk_sizes"], [len(chunk) for chunk in self.audio])

    def test_extract_is_byte_identical(self):
        out = os.path.join(self.tmp, "multi.m4a")
        container.extract(self.path, 1, out)
        info = container.parse_mp4(out)
        self.assertEqual(len(info["tracks"]), 1)
        index = info["tracks"][0]["index"]
        extracted = bytearray()
        with open(out, "rb") as fh:
            for offset, size in zip(index["chunk_offsets"], info["tracks"][0]["chunk_sizes"]):
                fh.seek(offset)
                extracted.extend(fh.read(size))
        self.assertEqual(bytes(extracted), b"".join(self.audio))


class TestMkv(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.audio = [b"AUDIOFRAME%02d" % i + bytes([0x11 + i]) * (64 + i * 5) for i in range(5)]
        self.video = [b"VIDEOFRAME%02d" % i + bytes([0x77 + i]) * (256 + i * 11) for i in range(5)]
        self.path = self.write("sample.mkv", synth.build_mkv(self.audio, self.video))

    def test_probe_lists_both_tracks(self):
        info = container.probe(self.path)
        self.assertEqual(info["family"], "ebml")
        self.assertEqual(len(info["tracks"]), 2)
        audio = info["audio_tracks"][0]
        self.assertEqual(audio["track_id"], 1)
        self.assertEqual(audio["codec"], "A_OPUS")
        self.assertEqual(audio["codec_name"], "Opus")
        self.assertEqual(audio["channels"], 2)

    def test_extract_keeps_only_audio_frames_byte_identical(self):
        """输出的每个块都必须是音轨帧，且字节与源一致。"""
        out = os.path.join(self.tmp, "out.mka")
        result = container.extract(self.path, 1, out)
        self.assertTrue(result["stream_copy"])

        info = container.parse_mkv(out)
        self.assertEqual(len(info["tracks"]), 1)
        self.assertEqual(info["tracks"][0]["kind"], "audio")
        self.assertEqual(info["tracks"][0]["codec"], "A_OPUS")

        frames = _collect_simple_blocks(out)
        self.assertEqual(len(frames), len(self.audio))
        for got, expected in zip(frames, self.audio):
            self.assertEqual(got[1], 1, "只应保留音轨 1 的块")
            self.assertEqual(got[2], expected)

        joined = b"".join(frame[2] for frame in frames)
        self.assertEqual(joined, b"".join(self.audio))

    def test_extract_drops_video_frames(self):
        out = os.path.join(self.tmp, "out2.mka")
        container.extract(self.path, 1, out)
        frames = _collect_simple_blocks(out)
        tracks_seen = {frame[1] for frame in frames}
        self.assertEqual(tracks_seen, {1})

    def test_extracted_file_is_smaller(self):
        out = os.path.join(self.tmp, "out3.mka")
        container.extract(self.path, 1, out)
        self.assertLess(os.path.getsize(out), os.path.getsize(self.path))

    def test_unknown_track_is_refused(self):
        out = os.path.join(self.tmp, "bad.mka")
        with self.assertRaises(container.ContainerError):
            container.extract(self.path, 42, out)

    def test_video_track_is_refused(self):
        out = os.path.join(self.tmp, "bad2.mka")
        with self.assertRaises(container.ContainerError):
            container.extract(self.path, 2, out)

    def test_suggested_output_is_mka(self):
        info = container.probe(self.path)
        name = container.suggested_output(self.path, info["audio_tracks"][0], self.tmp)
        self.assertTrue(name.endswith(".mka"), name)

    def test_webm_extension_also_supported(self):
        path = self.write("sample.webm", synth.build_mkv(self.audio, self.video))
        info = container.probe(path)
        self.assertEqual(info["family"], "ebml")


def _collect_simple_blocks(path):
    """从 MKV 里取出所有 SimpleBlock：``(timestamp, track_number, data)``。"""
    from app.container import (EBML_CLUSTER, EBML_SIMPLE_BLOCK, EBML_SEGMENT, EBML_TIMESTAMP,
                              Reader, _block_track_number, _decode_uint, iter_elements)

    frames = []
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        for element_id, payload_start, payload_end, _ in iter_elements(fh, 0, size):
            if element_id != EBML_SEGMENT:
                continue
            for child_id, child_start, child_end, _ in iter_elements(fh, payload_start, payload_end):
                if child_id != EBML_CLUSTER:
                    continue
                fh.seek(child_start)
                raw = fh.read(child_end - child_start)
                reader = Reader(raw)
                timestamp = 0
                for sub_id, sub_start, sub_end, _ in iter_elements(reader, 0, len(raw)):
                    payload = raw[sub_start:sub_end]
                    if sub_id == EBML_TIMESTAMP:
                        timestamp = _decode_uint(payload)
                    elif sub_id == EBML_SIMPLE_BLOCK:
                        number = _block_track_number(payload)
                        body = payload[len(_vint_bytes(number)) + 3:]
                        frames.append((timestamp, number, body))
            break
    return frames


def _vint_bytes(value):
    from app.container import encode_vint

    return encode_vint(value)


class TestEbmlPrimitives(unittest.TestCase):
    def test_vint_roundtrip(self):
        for value in (0, 1, 126, 127, 128, 16383, 16384, 1 << 20, (1 << 28) - 2):
            encoded = container.encode_vint(value)
            decoded, length, _ = container.read_vint(container.Reader(encoded))
            self.assertEqual(decoded, value, "vint 往返失败：%d" % value)
            self.assertEqual(length, len(encoded))

    def test_element_id_encoding_keeps_marker(self):
        for element_id in (0xE7, 0xA3, 0xAE, 0x1F43B675, 0x18538067, 0x1549A966):
            raw = container._encode_id(element_id)
            reader = container.Reader(raw)
            value, _, _ = container.read_vint(reader, keep_marker=True)
            self.assertEqual(value, element_id, "元素 ID 编码/解析不对称：%x" % element_id)

    def test_element_roundtrip(self):
        payload = b"hello-ebml"
        encoded = container.encode_element(0x4282, payload)
        reader = container.Reader(encoded)
        element_id, _, _ = container.read_vint(reader, keep_marker=True)
        size, _, _ = container.read_vint(reader)
        self.assertEqual(element_id, 0x4282)
        self.assertEqual(size, len(payload))
        self.assertEqual(reader.read(size), payload)

    def test_unknown_size_is_recognised(self):
        encoded = container._encode_id(container.EBML_SEGMENT) + b"\x01\xff\xff\xff\xff\xff\xff\xff"
        reader = container.Reader(encoded)
        element_id, _, _ = container.read_vint(reader, keep_marker=True)
        size, _, _ = container.read_vint(reader)
        self.assertEqual(element_id, container.EBML_SEGMENT)
        self.assertEqual(size, container.UNKNOWN_SIZE)


class TestBoxPrimitives(unittest.TestCase):
    def test_iter_boxes_finds_nested(self):
        inner = container.make_box(b"abcd", b"payload")
        outer = container.make_box(b"moov", inner)
        reader = container.Reader(outer)
        found = list(container.iter_boxes(reader, 0, len(outer)))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], b"moov")
        nested = list(container.iter_boxes(reader, found[0][2], found[0][3]))
        self.assertEqual(nested[0][0], b"abcd")

    def test_iter_boxes_handles_large_size_header(self):
        payload = b"x" * 32
        box = struct_pack_large(b"mdat", payload)
        reader = container.Reader(box)
        found = list(container.iter_boxes(reader, 0, len(box)))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], b"mdat")
        self.assertEqual(found[0][3] - found[0][2], len(payload))

    def test_iter_boxes_stops_on_garbage(self):
        reader = container.Reader(b"\x00\x00\x00\x04junkjunkjunk")
        self.assertEqual(list(container.iter_boxes(reader, 0, 16)), [])


def struct_pack_large(box_type, payload):
    import struct

    return struct.pack(">I", 1) + box_type + struct.pack(">Q", len(payload) + 16) + payload


class TestCodecNames(unittest.TestCase):
    def test_known_codecs(self):
        self.assertEqual(container.human_codec("mp4a"), "AAC")
        self.assertEqual(container.human_codec("A_OPUS"), "Opus")
        self.assertEqual(container.human_codec("A_FLAC"), "FLAC")

    def test_unknown_codec_passes_through(self):
        self.assertEqual(container.human_codec("zzzz"), "zzzz")
        self.assertEqual(container.human_codec(None), "未知")


if __name__ == "__main__":
    unittest.main()
