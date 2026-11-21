"""tts// uris and rendering through ovos-audio's remote rendering (OVOS-AUDIO-1 3.4)."""
import base64
from pathlib import Path

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovos_ocp_tts_plugin import (OCPTTSExtractor, RENDER, parse_tts_uri, tts_uri)

WAV = b"RIFF....WAVEfmt fake audio"


class FakeAudio:
    """Answers ovos.utterance.speak.b64 the way ovos-audio's handle_b64_audio does."""

    def __init__(self, bus, topic="ovos.audio.speech", audio=WAV):
        self.requests = []
        self.topic = topic
        self.audio = audio
        self.bus = bus
        bus.on(RENDER, self.on_render)

    def on_render(self, message):
        self.requests.append(message)
        if self.audio is None:
            return  # ovos-audio not running
        self.bus.emit(message.forward(self.topic, {
            "audio": base64.b64encode(self.audio).decode(),
            "listen": False, "tts_id": "ovos-tts-plugin-piper",
            "utterance": message.data["utterance"]}))


@pytest.fixture
def bus():
    return FakeBus()


@pytest.fixture
def xtract(bus, tmp_path):
    x = OCPTTSExtractor(bus=bus, cache_dir=tmp_path)
    x.timeout = 2
    return x


def test_uri_round_trip():
    text = "Far out in the ocean, the water is as blue as cornflowers / and clear."
    uri = tts_uri(text, "en-US")
    assert uri.startswith("tts//en-US/")
    assert parse_tts_uri(uri) == ("en-US", text)


@pytest.mark.parametrize("uri", ["", "youtube//x", "tts//", "tts//en-US/", "tts///text", "tts//en-US"])
def test_not_a_reading(uri):
    assert parse_tts_uri(uri) is None


def test_claims_the_tts_sei(xtract):
    assert xtract.supported_seis == ["tts"]
    assert xtract.validate_uri(tts_uri("Hello.", "en-US"))
    assert not xtract.validate_uri("youtube//https://example.com")


def test_renders_through_ovos_audio(bus, xtract):
    audio = FakeAudio(bus)
    meta = xtract.extract_stream(tts_uri("Langt ude i havet.", "da-DK"))
    path = Path(meta["uri"][len("file://"):])
    assert meta["uri"].startswith("file://") and path.read_bytes() == WAV
    [request] = audio.requests
    assert request.data == {"utterance": "Langt ude i havet.", "lang": "da-DK", "listen": False}
    # its own session: the device's default session (and its language) is left alone
    assert request.context["session"] == {"session_id": "ovos-ocp-tts-plugin", "lang": "da-DK"}


def test_understands_the_legacy_answer(bus, xtract):
    FakeAudio(bus, topic="speak:b64_audio.response")
    assert xtract.extract_stream(tts_uri("Hello.", "en-US"))["uri"].startswith("file://")


def test_ignores_answers_to_other_requests(bus, xtract):
    FakeAudio(bus)
    # someone else's rendering arrives first; it must not be taken for ours
    bus.emit(Message("ovos.audio.speech", {"audio": base64.b64encode(b"other").decode()},
                     {"tts_render_id": "not-ours"}))
    path = xtract.extract_stream(tts_uri("Hello.", "en-US"))["uri"][len("file://"):]
    assert Path(path).read_bytes() == WAV


def test_no_ovos_audio_means_no_stream(bus, xtract):
    FakeAudio(bus, audio=None)
    xtract.timeout = 0.2
    assert xtract.extract_stream(tts_uri("Hello.", "en-US")) is None


def test_empty_audio_means_no_stream(bus, xtract):
    FakeAudio(bus, audio=b"")
    assert xtract.extract_stream(tts_uri("Hello.", "en-US")) is None


def test_same_sentence_same_file(bus, xtract):
    FakeAudio(bus)
    a = xtract.extract_stream(tts_uri("Hello.", "en-US"))["uri"]
    b = xtract.extract_stream(tts_uri("Hello.", "en-US"))["uri"]
    c = xtract.extract_stream(tts_uri("Hello.", "en-GB"))["uri"]
    assert a == b != c


def test_listeners_are_removed(bus, xtract):
    FakeAudio(bus)
    before = bus.ee.listeners("ovos.audio.speech")[:]
    xtract.extract_stream(tts_uri("Hello.", "en-US"))
    assert bus.ee.listeners("ovos.audio.speech") == before


def test_through_the_stream_handler(bus, xtract, monkeypatch):
    """ovos-plugin-manager's StreamHandler, as ovos-media uses it, resolves the uri."""
    from ovos_plugin_manager import ocp
    FakeAudio(bus)
    monkeypatch.setattr(ocp, "find_ocp_plugins", lambda: {"ovos-ocp-tts-plugin": lambda: xtract})
    meta = ocp.StreamHandler().extract_stream(tts_uri("Hello.", "en-US"), video=False)
    assert meta["uri"].startswith("file://")
