# Deckster default sound pack licensing

Every sound in this directory is distributed under the
[CC0 1.0 Universal dedication](https://creativecommons.org/publicdomain/zero/1.0/).
Attribution is not legally required, but the source information below is kept for
auditing and as a courtesy to the original creators.

## Kenney Interface Sounds

- Creator: Kenney
- License: CC0 1.0
- Official page: https://kenney.nl/assets/interface-sounds
- Reviewed download: `kenney_interface-sounds.zip`
- Download SHA-256: `f2193d072726d6758a5f7871b2dcc54dcce0d5c35c6f0a62f92549b327c81232`
- Selected upstream files: `confirmation_001.ogg`, `error_006.ogg`,
  `scratch_004.ogg`, `pluck_001.ogg`, and `bong_001.ogg`
- Deckster changes: decoded to mono 48 kHz PCM WAV, peak-normalized, and given
  short edge fades; no other archive members are extracted or shipped.

These become `success.wav`, `wrong.wav`, `record-scratch.wav`, `pop.wav`, and
`bell.wav` respectively.

## Cricket sounds

- Creator: syncopika
- License: CC0 1.0
- Official page: https://opengameart.org/content/cricket-sounds
- Reviewed download: `cricketsounds090613.wav`
- Download SHA-256: `ecc088a3d1ab967202b63a92836dadc47bd08c8ab3f17d8527f620264aa5faf9`
- Deckster changes: five-second excerpt beginning at 19 seconds, converted to
  mono 48 kHz PCM WAV, peak-normalized, and edge-faded.

## Deckster synthesized sounds

`air-horn.wav`, `rimshot.wav`, `sad-trombone.wav`, `drum-roll.wav`,
`censor-bleep.wav`, and `applause.wav` are deterministic, original synthesis
created by `tools/build_default_sound_pack.py` specifically for Deckster. They
are dedicated to the public domain under CC0 1.0.

Deckster is not affiliated with or endorsed by Discord. These are independent
recordings and synthesized effects, not copies of Discord's sound assets.
