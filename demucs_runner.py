#!/usr/bin/env python3
"""
Wrapper that patches torchaudio.load to use soundfile instead of torchcodec,
then runs demucs normally. Needed because torchaudio on Python 3.14 routes
all audio loading through torchcodec which isn't available.
"""
import sys
import soundfile as sf
import torch
import torchaudio


def _soundfile_load(uri, frame_offset=0, num_frames=-1, normalize=True,
                    channels_first=True, format=None, backend=None):
    data, sr = sf.read(str(uri), always_2d=True)  # (frames, channels)
    tensor = torch.from_numpy(data.T).float()       # (channels, frames)
    if frame_offset > 0:
        tensor = tensor[:, frame_offset:]
    if num_frames > 0:
        tensor = tensor[:, :num_frames]
    return tensor, sr


def _soundfile_save(uri, src, sample_rate, channels_first=True, **kwargs):
    data = src.numpy()
    if channels_first:
        data = data.T  # (channels, frames) → (frames, channels)
    sf.write(str(uri), data, sample_rate)


torchaudio.load = _soundfile_load
torchaudio.save = _soundfile_save

from demucs.separate import main
sys.exit(main())
