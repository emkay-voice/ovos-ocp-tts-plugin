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

Rendering ahead: the player asks for one sentence at a time, and only when
the one before it has ended, so each sentence would wait for its own
rendering. The extractor therefore listens for the playlist it is about to
be asked about (``ovos.common_play.play``) and, whenever a sentence is asked
for, renders the next few in the background. When the player gets there,
the sentence is ready. Setting ``tts_prefetch`` (default 2; 0 turns it off).

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
PREFETCH = 2  # sentences rendered ahead of the one asked for
PLAY = "ovos.common_play.play"  # carries the playlist the player will ask about


class _Rendering:
    """One sentence's rendering: asked for, in progress, or done (``path``,
    None when it failed)."""

    def __init__(self):
        self.done = threading.Event()
        self.path = None

    def usable(self) -> bool:
        return not self.done.is_set() or bool(self.path and os.path.exists(self.path))


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
        # ovos-plugin-manager makes extractors with no settings; then they
        # come from the "media" section of mycroft.conf, next to the player's
        if ocp_settings is None:
            try:
                from ovos_config import Configuration
                ocp_settings = Configuration().get("media", {})
            except Exception:
                ocp_settings = {}
        super().__init__(ocp_settings)
        self._bus = bus
        self._bus_lock = threading.Lock()
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.timeout = self.ocp_settings.get("tts_render_timeout", RENDER_TIMEOUT)
        self.prefetch = max(0, int(self.ocp_settings.get("tts_prefetch", PREFETCH)))
        self._lock = threading.Lock()
        self._playlist = []   # the tts// uris of the playlist being played, in order
        self._cursor = 0      # where in it the player last asked
        self._renderings = {}  # uri -> _Rendering, for the sentences around the cursor
        if self.prefetch:
            if bus is not None:
                self._listen()
            else:
                # the play request arrives before the first sentence is asked
                # for, so listen from the start; connecting must not hold up
                # the player loading its extractors
                threading.Thread(target=self._listen, daemon=True,
                                 name="ovos-ocp-tts-plugin-listen").start()

    def _listen(self):
        try:
            self.bus.on(PLAY, self._on_play)
        except Exception as e:
            LOG.warning(f"not rendering ahead, no messagebus: {e}")

    def _on_play(self, message):
        """Remember the tts// uris of a playlist the player is about to play,
        and start on its first sentences."""
        data = message.data or {}
        entries = data.get("playlist") or [data.get("media")]
        uris = [e["uri"] for e in entries
                if isinstance(e, dict) and isinstance(e.get("uri"), str) and parse_tts_uri(e["uri"])]
        if not uris:
            return
        start = data.get("media", {}).get("uri") if isinstance(data.get("media"), dict) else None
        with self._lock:
            self._playlist = uris
            self._cursor = uris.index(start) if start in uris else 0
            self._renderings = {u: r for u, r in self._renderings.items() if u in uris}
            upcoming = uris[self._cursor:self._cursor + 1 + self.prefetch]
        self._render_ahead(upcoming)

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
        rendering, mine = self._claim(uri)
        if mine:
            self._finish(uri, rendering)
        else:
            rendering.done.wait(self.timeout)  # rendered ahead, or still being
        self._render_ahead(self._upcoming(uri))
        if not rendering.path:
            return None
        return {"uri": f"file://{rendering.path}"}

    # --- rendering ahead -------------------------------------------------------------

    def _claim(self, uri):
        """The rendering of ``uri``: one already done or under way, or a new
        one this caller has to do (``mine``)."""
        with self._lock:
            rendering = self._renderings.get(uri)
            if rendering is not None and rendering.usable():
                return rendering, False
            rendering = self._renderings[uri] = _Rendering()
            return rendering, True

    def _finish(self, uri, rendering):
        try:
            rendering.path = self.render(*parse_tts_uri(uri)[::-1])
        finally:
            rendering.done.set()
            if not rendering.path:
                with self._lock:  # a failed one is tried again when asked for
                    if self._renderings.get(uri) is rendering:
                        del self._renderings[uri]

    def _upcoming(self, uri):
        """The sentences after ``uri`` in the playlist being played; also
        moves the cursor there and forgets renderings left behind."""
        with self._lock:
            if uri not in self._playlist:
                # not from a playlist it heard about: nothing to look ahead to
                self._renderings = {u: r for u, r in self._renderings.items() if u == uri}
                return []
            # a sentence can occur twice: take the one nearest the cursor
            hits = [i for i, u in enumerate(self._playlist) if u == uri]
            self._cursor = min(hits, key=lambda i: abs(i - self._cursor))
            keep = set(self._playlist[max(0, self._cursor - 1):self._cursor + 1 + self.prefetch])
            self._renderings = {u: r for u, r in self._renderings.items() if u in keep}
            return self._playlist[self._cursor + 1:self._cursor + 1 + self.prefetch]

    def _render_ahead(self, uris):
        """Render ``uris`` in order on a background thread, skipping any
        already done or under way."""
        todo = []
        for uri in uris:
            rendering, mine = self._claim(uri)
            if mine:
                todo.append((uri, rendering))
        if todo:
            threading.Thread(target=lambda: [self._finish(u, r) for u, r in todo],
                             daemon=True, name="ovos-ocp-tts-plugin-ahead").start()

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
