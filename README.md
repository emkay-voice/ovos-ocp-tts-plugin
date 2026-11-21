# ovos-ocp-tts-plugin

An OCP stream extractor for text read aloud. A media entry whose uri is

```
tts//en-US/Once%20upon%20a%20time%2C%20there%20was%20a%20little%20mermaid.
```

plays as that sentence, spoken in the device's own voice.

It does not load a TTS engine of its own. It asks **ovos-audio** to render the sentence without playing it (the
remote rendering of OVOS-AUDIO-1 3.4: `ovos.utterance.speak.b64`, answered by `ovos.audio.speech`), writes the audio
to a cache file and hands the player that file. So the voice, its settings and ovos-audio's TTS cache are the ones the
device already uses, and there is never a second voice model in memory.

Made for [Emkay](https://github.com/emkay-voice/emkay), where reading fairy tales aloud should work like any other
media, and meant for OpenVoiceOS as a whole. The design is discussed in
[OpenVoiceOS/ovos-media#240](https://github.com/OpenVoiceOS/ovos-media/issues/240).

## How a reading plays

One uri is one sentence, or a short paragraph. A reading is a playlist of such entries, so the media player's own
controls do the rest:

- pause and resume, stop
- next and previous sentence ("go back a bit")
- the position is simply the playlist index, so a reading can be picked up where it stopped
- the now-playing screen shows the title, author and picture of the entries

Whoever has the text builds the playlist, for example the common reading pipeline, which keeps its own words for
"read me …", "continue reading" and so on, in every language it speaks:

```python
from ovos_utils.ocp import MediaEntry, PlaybackType, Playlist
from ovos_ocp_tts_plugin import tts_uri

reading = Playlist(title="The Little Mermaid", artist="H. C. Andersen", image=picture)
for sentence in sentences:
    reading.add_entry(MediaEntry(uri=tts_uri(sentence, "en-US"), title="The Little Mermaid",
                                 artist="H. C. Andersen", image=picture,
                                 playback=PlaybackType.AUDIO))
```

In the mediavocab terms ovos-media uses, a tale is a `BOOK` work; reading it aloud is how it is delivered, so it is a
release whose uri is `tts//…`.

## Install

```bash
pip install ovos-ocp-tts-plugin
```

It is picked up by every player that uses OVOS' stream extractors (`opm.ocp.extractor`), such as ovos-media. ovos-audio
must be running, as it always is on an OVOS device.

## Configuration

None needed. Under the player's OCP settings, `tts_render_timeout` (seconds, default 60) is how long to wait for
ovos-audio to render a sentence. Rendered files are kept in `~/.cache/ovos-ocp-tts-plugin` for a day; ovos-audio's own
TTS cache is what makes a repeated sentence fast.

## Development

```bash
pip install -e ".[test]"
pytest tests
```

## License

Apache-2.0
