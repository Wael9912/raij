"""Generated visuals (Phase 21): the channel's own animated maps, data charts and stat cards.

A script beat may carry a `visual` spec (written by the script model, validated by `spec.validate`); assemble
renders it here into an mp4 under assets/generated/visuals/ that plays for the beat's length, instead of
anyone else's pictures. Everything is drawn with Pillow (Arabic shaped by raqm, like the subtitles) and piped
to ffmpeg as raw frames — no new dependencies, $0.

  map   — Natural Earth countries (public domain, assets/geo/, `tools/fetch_geo.py`): the camera eases from a
          wide view onto the focus countries, which light up in gold; optional markers (ports, straits,
          cities from places.json) and routes that draw themselves.
  chart — bar or line chart whose values come from the World Bank API (keyless) or from numbers the story
          card itself states (checked like the script's figures); the source is printed on the frame.
  stat  — one big figure counting up, with a label and the source.
"""
