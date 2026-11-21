"""OCP stream extractor for text read aloud: ``tts//<lang>/<text>``.

A media entry whose uri is ``tts//en-US/Once%20upon%20a%20time.`` plays as
that sentence spoken in the device's own voice. The extractor does not load a
TTS engine: it asks ovos-audio to render the sentence without playing it
(OVOS-AUDIO-1 remote rendering, ``ovos.utterance.speak.b64`` answered by
``ovos.audio.speech``), writes the audio to a cache file and hands the player
that file. So the voice, its settings and ovos-audio's TTS cache are the ones
the device already uses, and no second voice model is loaded.

One uri is one sentence (or a short paragraph). A reading is a playlist of
such entries, so the player's own controls do the rest: pause and resume,
next and previous sentence, and the position is the playlist index.

See OpenVoiceOS/ovos-media#240.
"""
import base64
import hashlib
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

from ovos_bus_client.message import Message
from ovos_plugin_manager.templates.ocp import OCPStreamExtractor
from ovos_utils import classproperty
from ovos_utils.log import LOG
from ovos_utils.process_utils import RuntimeRequirements

SEI = "tts"
# OVOS-AUDIO-1 3.4/4.3: render without playing, and the answer; the legacy
# names are what older ovos-audio versions use
RENDER = "ovos.utterance.speak.b64"
RENDERED = ("ovos.audio.speech", "speak:b64_audio.response")
# a session of its own, so asking for a language never touches the device's
# default session
SESSION_ID = "ovos-ocp-tts-plugin"
RENDER_TIMEOUT = 60  # seconds; a long sentence on a slow device
CACHE_MAX_AGE = 24 * 3600  # seconds a rendered file is kept


def tts_uri(text: str, lang: str) -> str:
    """The uri that plays ``text`` spoken in ``lang``."""
    return f"{SEI}//{quote(lang, safe='')}/{quote(text, safe='')}"


def parse_tts_uri(uri: str) -> Optional[tuple]:
    """``(lang, text)`` of a ``tts//`` uri, or None when it is not one."""
    prefix = f"{SEI}//"
    if not uri or not uri.startswith(prefix):
        return None
    lang, _, text = uri[len(prefix):].partition("/")
    lang, text = unquote(lang).strip(), unquote(text).strip()
    if not lang or not text:
        return None
    return lang, text


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "ovos-ocp-tts-plugin"


class OCPTTSExtractor(OCPStreamExtractor):

    def __init__(self, ocp_settings=None, bus=None, cache_dir=None):
        super().__init__(ocp_settings)
        self._bus = bus
        self._bus_lock = threading.Lock()
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.timeout = self.ocp_settings.get("tts_render_timeout", RENDER_TIMEOUT)

    @classproperty
    def runtime_requirements(cls):
        # rendering is local: whatever ovos-audio's TTS needs, this needs nothing
        return RuntimeRequirements(internet_before_load=False,
                                   network_before_load=False,
                                   requires_internet=False,
                                   requires_network=False,
                                   no_internet_fallback=True,
                                   no_network_fallback=True)

    @classproperty
    def supported_seis(cls):
        return [SEI]

    @property
    def bus(self):
        """The messagebus, connected on first use (the player loads every
        extractor at start, most of which never see a ``tts//`` uri)."""
        with self._bus_lock:
            if self._bus is None:
                from ovos_bus_client import MessageBusClient
                bus = MessageBusClient()
                bus.run_in_thread()
                bus.connected_event.wait(10)
                self._bus = bus
            return self._bus

    def extract_stream(self, uri, video=True):
        parsed = parse_tts_uri(uri)
        if not parsed:
            return None
        lang, text = parsed
        path = self.render(text, lang)
        if not path:
            return None
        return {"uri": f"file://{path}"}

    def render(self, text: str, lang: str) -> Optional[str]:
        """Ask ovos-audio for ``text`` spoken in ``lang``; the cache file's
        path, or None when nothing answered in time."""
        request_id = str(uuid.uuid4())
        answer = {}
        done = threading.Event()

        def on_rendered(message):
            if message.context.get("tts_render_id") != request_id or done.is_set():
                return
            answer.update(message.data)
            done.set()

        for topic in RENDERED:
            self.bus.on(topic, on_rendered)
        try:
            self.bus.emit(Message(RENDER, {"utterance": text, "lang": lang, "listen": False},
                                  {"source": "ovos-ocp-tts-plugin",
                                   "destination": ["audio"],
                                   "session": {"session_id": SESSION_ID, "lang": lang},
                                   "tts_render_id": request_id}))
            if not done.wait(self.timeout):
                LOG.error(f"no audio from ovos-audio within {self.timeout}s for: {text!r}")
                return None
        finally:
            for topic in RENDERED:
                self.bus.remove(topic, on_rendered)
        try:
            audio = base64.b64decode(answer.get("audio") or "")
        except ValueError:
            audio = b""
        if not audio:
            LOG.error(f"ovos-audio answered without audio for: {text!r}")
            return None
        return self._store(audio, text, lang, answer.get("tts_id") or "")

    def _store(self, audio: bytes, text: str, lang: str, tts_id: str) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._prune()
        key = hashlib.sha1(f"{tts_id}\n{lang}\n{text}".encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{key}.wav"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(audio)
        tmp.replace(path)
        return str(path)

    def _prune(self):
        """Drop rendered files older than CACHE_MAX_AGE: ovos-audio keeps its
        own TTS cache, these are only the copies handed to the player."""
        cutoff = time.time() - CACHE_MAX_AGE
        for old in self.cache_dir.glob("*.wav"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
