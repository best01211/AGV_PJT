"""Run the NVlabs model on already rectified BGR image pairs."""
import os
import sys
from collections import defaultdict
from typing import Any

import cv2
import numpy as np


class FoundationMatcher:
    def __init__(self, repository, checkpoint, iterations=16, input_scale=1.0):
        repository = repository.resolve(strict=True)
        checkpoint = checkpoint.resolve(strict=True)
        config = checkpoint.with_name('cfg.yaml')
        if not (repository / 'core' / 'foundation_stereo.py').is_file():
            raise ValueError('FoundationStereo repository is missing core/foundation_stereo.py')
        if not config.is_file():
            raise ValueError(f'Missing model config: {config}')
        os.environ.setdefault('XFORMERS_DISABLED', '1')
        sys.path.insert(0, str(repository))
        import torch
        from omegaconf import DictConfig, ListConfig, OmegaConf
        from omegaconf.base import ContainerMetadata, Metadata
        from omegaconf.nodes import AnyNode
        from core.foundation_stereo import FoundationStereo
        from core.utils.utils import InputPadder

        if not torch.cuda.is_available():
            raise RuntimeError('FoundationStereo requires CUDA in this setup')
        self.torch = torch
        self.padder_type = InputPadder
        self.iterations = iterations
        self.input_scale = input_scale
        settings = OmegaConf.load(config)
        if 'vit_size' not in settings:
            settings.vit_size = 'vitl'
        self.model = FoundationStereo(settings)
        # The official checkpoint includes NumPy and OmegaConf training metadata.
        checkpoint_types = [
            (np._core.multiarray.scalar, 'numpy.core.multiarray.scalar'),
            np.dtype, type(np.dtype('float64')), type(np.dtype('float32')),
            DictConfig, ListConfig, ContainerMetadata, Metadata, AnyNode,
            list, dict, int, defaultdict, Any,
        ]
        with torch.serialization.safe_globals(checkpoint_types):
            weights = torch.load(checkpoint, map_location='cpu', weights_only=True)
        self.model.load_state_dict(weights['model'])
        self.model.cuda().eval()

    def __call__(self, left, right):
        torch = self.torch
        height, width = left.shape[:2]
        if self.input_scale != 1.0:
            inference_size = (round(width * self.input_scale), round(height * self.input_scale))
            left, right = (cv2.resize(frame, inference_size, interpolation=cv2.INTER_AREA)
                           for frame in (left, right))
        # Flipping both swapped images gives a positive disparity in the reverse view.
        pairs = ((left, right), (cv2.flip(right, 1), cv2.flip(left, 1)))
        outputs = []
        with torch.inference_mode(), torch.autocast('cuda'):
            for pair in pairs:
                inputs = [
                    torch.from_numpy(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    .to(device='cuda', dtype=torch.float32).permute(2, 0, 1)[None]
                    for frame in pair
                ]
                padder = self.padder_type(inputs[0].shape, divis_by=32, force_square=False)
                prediction = self.model(*padder.pad(*inputs), iters=self.iterations, test_mode=True)
                outputs.append(padder.unpad(prediction.float()).cpu().numpy().reshape(left.shape[:2]))
        forward, reverse = outputs[0], -np.flip(outputs[1], axis=1)
        if self.input_scale != 1.0:
            pixel_scale = width / left.shape[1]
            forward, reverse = (
                cv2.resize(disparity, (width, height), interpolation=cv2.INTER_LINEAR) * pixel_scale
                for disparity in (forward, reverse)
            )
        return forward, reverse
