"""
Masked applied on a predefined set of images
"""
from collections.abc import Sequence
from typing import Tuple

import numpy as np
import torch as ch
from numpy import dtype
from numpy.random import rand
from dataclasses import replace
from typing import Callable, Optional, Tuple
from ..pipeline.allocation_query import AllocationQuery
from ..pipeline.operation import Operation
from ..pipeline.state import State
from ..pipeline.compiler import Compiler

def ch_dtype_from_numpy(dtype):
    return ch.from_numpy(np.zeros((), dtype=dtype)).dtype

class NormalizeImage(Operation):
    """Perform Image Normalization.
    Operates on raw arrays (not tensors).

    Parameters
    ----------
    mean: np.ndarray
        The mean vector
    std: np.ndarray
        The standard deviation
    type: np.dtype
        The desired output type for the result
    """

    def __init__(self, mean: np.ndarray, std: np.ndarray,
                 type: np.dtype):
        super().__init__()
        table = (np.arange(256)[:, None] - mean[None, :]) / std[None, :]
        self.original_dtype = type
        if type == np.float16:
            type = np.int16
        self.dtype = type
        table = table.astype(type)
        self.lookup_table = table
        self.previous_shape = None
        self.mode = 'cpu'

    def generate_code(self) -> Callable:
        if self.mode == 'cpu':
            return self.generate_code_cpu()
        return self.generate_code_gpu()
        
    def generate_code_gpu(self) -> Callable:

        # We only import cupy if it's truly needed
        import cupy as cp

        tn = np.zeros((), dtype=self.dtype).dtype.name
        kernel = cp.ElementwiseKernel(f'uint8 input, raw {tn} table', f'{tn} output', 'output = table[input * 3 + i % 3];')
        final_type = ch_dtype_from_numpy(self.original_dtype)
        s = self
        def normalize_convert(images, result):
            B, C, H, W = images.shape
            table = self.lookup_table.view(-1)
            assert images.is_contiguous(memory_format=ch.channels_last), 'Images need to be in channel last'
            result_c = result.view(-1)
            images = images.permute(0, 2, 3, 1).view(-1)
            kernel(images, table, result_c)

            # Mark the result as channel last
            final_result = result.reshape(B, H, W, C).permute(0, 3, 1, 2)

            assert final_result.is_contiguous(memory_format=ch.channels_last), 'Images need to be in channel last'

            return final_result.view(final_type)
        
        return normalize_convert

    def generate_code_cpu(self) -> Callable:

        table = self.lookup_table.view(dtype=self.dtype)
        my_range = Compiler.get_iterator()
        previous_shape = self.previous_shape

        def normalize_convert(images, result, indices):
            result_flat = result.reshape(result.shape[0], -1, 3)
            num_pixels = result_flat.shape[1]
            for i in my_range(len(indices)):
                image = images[i].reshape(num_pixels, 3)
                for px in range(num_pixels):
                    # Just in case llvm forgets to unroll this one
                    result_flat[i, px, 0] = table[image[px, 0], 0]
                    result_flat[i, px, 1] = table[image[px, 1], 0]
                    result_flat[i, px, 2] = table[image[px, 2], 0]
            return result

        normalize_convert.is_parallel = True
        normalize_convert.with_indices = True
        return normalize_convert

    def declare_state_and_memory(self, previous_state: State) -> Tuple[State, Optional[AllocationQuery]]:

        if previous_state.device == ch.device('cpu'):
            new_state = replace(previous_state, jit_mode=True, dtype=self.dtype)
            return new_state, AllocationQuery(
                shape=previous_state.shape,
                dtype=self.dtype,
                device=previous_state.device
            )

        else:
            self.mode = 'gpu'
            new_state = replace(previous_state, dtype=self.dtype)

            gpu_type = ch_dtype_from_numpy(self.dtype)


            # Copy the lookup table into the proper device
            try:
                self.lookup_table = ch.from_numpy(self.lookup_table)
            except TypeError:
                pass  # This is alredy a tensor
            self.lookup_table = self.lookup_table.to(previous_state.device)

            return new_state, AllocationQuery(
                shape=previous_state.shape,
                device=previous_state.device,
                dtype=gpu_type
            )