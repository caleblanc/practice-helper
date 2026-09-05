#!/usr/bin/env python3
"""Run demucs, working around torchaudio when it is present and in the way.

Older demucs routed all audio loading through torchaudio, which on Python 3.14
in turn routes through torchcodec -- a package that often is not installed. The
shim below redirects torchaudio to soundfile so separation still works.

demucs 4.1 does not need torchaudio at all, and it is not a hard dependency, so
the shim is applied only when torchaudio is actually importable. Importing it
unconditionally made stem extraction fail outright on any environment that
simply did not have it.
"""
import sys

try:
    import soundfile as sf
    import torch
    import torchaudio
except ImportError:
    pass                      # nothing to patch; demucs handles its own loading
else:
    def _soundfile_load(uri, frame_offset=0, num_frames=-1, normalize=True,
                        channels_first=True, format=None, backend=None):
        data, sr = sf.read(str(uri), always_2d=True)   # (frames, channels)
        tensor = torch.from_numpy(data.T).float()      # (channels, frames)
        if frame_offset > 0:
            tensor = tensor[:, frame_offset:]
        if num_frames > 0:
            tensor = tensor[:, :num_frames]
        return tensor, sr

    def _soundfile_save(uri, src, sample_rate, channels_first=True, **kwargs):
        data = src.numpy()
        if channels_first:
            data = data.T                              # (channels, frames) -> (frames, channels)
        sf.write(str(uri), data, sample_rate)

    torchaudio.load = _soundfile_load
    torchaudio.save = _soundfile_save

from demucs.separate import main

sys.exit(main())
