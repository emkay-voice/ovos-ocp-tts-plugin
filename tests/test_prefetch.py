"""Rendering ahead: the sentences after the one asked for are rendered in the
background, so the player finds them ready."""
import base64
import threading
import time

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from ovos_ocp_tts_plugin import PLAY, RENDER, OCPTTSExtractor, tts_uri

SENTENCES = [f"Sentence {i}." for i in range(1, 7)]
URIS = [tts_uri(s, "en-US") for s in SENTENCES]


class SlowAudio:
    """ovos-audio taking `seconds` to render each sentence, one at a time."""

    def __init__(self, bus, seconds=0.3):
        self.bus, self.seconds = bus, seconds
        self.rendered = []
        self.lock = threading.Lock()
        bus.on(RENDER, self.on_render)

    def on_render(self, message):
        def work():
            with self.lock:
                time.sleep(self.seconds)
                self.rendered.append(message.data["utterance"])
                self.bus.emit(message.forward("ovos.audio.speech", {
                    "audio": base64.b64encode(b"wav " + message.data["utterance"].encode()).decode(),
                    "tts_id": "slow", "utterance": message.data["utterance"]}))
        threading.Thread(target=work, daemon=True).start()


def _play(bus, start=0):
    playlist = [{"uri": u, "title": "Story"} for u in URIS]
    bus.emit(Message(PLAY, {"media": playlist[start], "playlist": playlist}))


@pytest.fixture
def bus():
    return FakeBus()


def _xtract(bus, tmp_path, **settings):
    x = OCPTTSExtractor(settings, bus=bus, cache_dir=tmp_path)
    x.timeout = 5
    return x


def _timed(xtract, uri):
    t = time.monotonic()
    meta = xtract.extract_stream(uri)
    return meta, time.monotonic() - t


def test_the_next_sentences_are_ready_when_asked_for(bus, tmp_path):
    audio = SlowAudio(bus)
    xtract = _xtract(bus, tmp_path)
    _play(bus)
    first, _ = _timed(xtract, URIS[0])
    assert first["uri"].startswith("file://")
    time.sleep(1.0)  # the first sentence plays; the next two render meanwhile
    for uri in URIS[1:3]:
        meta, took = _timed(xtract, uri)
        assert meta["uri"].startswith("file://")
        assert took < 0.1, f"{uri} took {took:.2f}s: it was not rendered ahead"


def test_never_more_than_prefetch_ahead(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.05)
    xtract = _xtract(bus, tmp_path)
    _play(bus)
    xtract.extract_stream(URIS[0])
    time.sleep(0.5)
    assert audio.rendered == SENTENCES[:3]  # the one asked for and two ahead


def test_every_sentence_is_rendered_once(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.05)
    xtract = _xtract(bus, tmp_path)
    _play(bus)
    for uri in URIS:
        assert xtract.extract_stream(uri)
    time.sleep(0.3)
    assert sorted(audio.rendered) == sorted(SENTENCES)


def test_asking_while_it_renders_ahead_waits_for_that_rendering(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.3)
    xtract = _xtract(bus, tmp_path)
    _play(bus)  # starts on sentences 1-3 at once
    meta, _ = _timed(xtract, URIS[0])
    assert meta["uri"].startswith("file://")
    assert audio.rendered.count(SENTENCES[0]) == 1


def test_a_reading_started_midway_renders_from_there(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.05)
    xtract = _xtract(bus, tmp_path)
    _play(bus, start=3)
    time.sleep(0.4)
    assert audio.rendered == SENTENCES[3:6]


def test_going_back_a_sentence_needs_no_new_rendering(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.05)
    xtract = _xtract(bus, tmp_path)
    _play(bus)
    xtract.extract_stream(URIS[0])
    xtract.extract_stream(URIS[1])
    time.sleep(0.4)
    before = len(audio.rendered)
    _, took = _timed(xtract, URIS[0])
    assert took < 0.1 and len(audio.rendered) == before


def test_prefetch_off(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.05)
    xtract = _xtract(bus, tmp_path, tts_prefetch=0)
    _play(bus)
    xtract.extract_stream(URIS[0])
    time.sleep(0.3)
    assert audio.rendered == SENTENCES[:1]


def test_a_failed_rendering_is_tried_again(bus, tmp_path):
    xtract = _xtract(bus, tmp_path)
    xtract.timeout = 0.2
    assert xtract.extract_stream(URIS[0]) is None  # nothing answers yet
    SlowAudio(bus, seconds=0.01)
    assert xtract.extract_stream(URIS[0])["uri"].startswith("file://")


def test_other_playlists_are_ignored(bus, tmp_path):
    audio = SlowAudio(bus, seconds=0.01)
    xtract = _xtract(bus, tmp_path)
    bus.emit(Message(PLAY, {"media": {"uri": "https://example.com/a.mp3"},
                            "playlist": [{"uri": "https://example.com/a.mp3"}]}))
    time.sleep(0.1)
    assert audio.rendered == []
