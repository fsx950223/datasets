# coding=utf-8
# Copyright 2019 The TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sequence feature."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf

from tensorflow_datasets.core import utils
from tensorflow_datasets.core.features import feature as feature_lib


class Sequence(feature_lib.FeatureConnector):
  """Composite `FeatureConnector` for a `dict` where each value is a list.

  `Sequence` correspond to sequence of `tfds.features.FeatureConnector`. At
  generation time, a list for each of the sequence element is given. The output
  of `tf.data.Dataset` will batch all the elements of the sequence together.

  If the length of the sequence is static and known in advance, it should be
  specified in the constructor using the `length` param.

  Note that `SequenceDict` do not support features which are of type
  `tf.io.FixedLenSequenceFeature`.

  Example:
  At construction time:
  ```
  tfds.features.Sequence(tfds.features.Image(), length=NB_FRAME)
  ```

  or:
  ```
  tfds.features.Sequence({
      'frame': tfds.features.Image(shape=(64, 64, 3))
      'action': tfds.features.ClassLabel(['up', 'down', 'left', 'right'])
  }, length=NB_FRAME)
  ```

  During data generation:
  ```
  yield {
      'frame': np.ones(shape=(NB_FRAME, 64, 64, 3)),
      'action': ['left', 'left', 'up', ...],
  }
  ```

  Tensor returned by `.as_dataset()`:
  ```
  {
      'frame': tf.Tensor(shape=(NB_FRAME, 64, 64, 3), dtype=tf.uint8),
      'action': tf.Tensor(shape=(NB_FRAME,), dtype=tf.int64),
  }
  ```

  At generation time, you can specify a list of features dict, a dict of list
  values or a stacked numpy array. The lists will automatically be distributed
  into their corresponding `FeatureConnector`.

  """

  def __init__(self, feature, length=None, **kwargs):
    """Construct a sequence dict.

    Args:
      feature: `dict`, the features to wrap
      length: `int`, length of the sequence if static and known in advance
      **kwargs: `dict`, constructor kwargs of `tfds.features.FeaturesDict`
    """
    # Convert {} => FeaturesDict, tf.int32 => Tensor(shape=(), dtype=tf.int32)
    self._feature = feature_lib.to_feature(feature)
    self._length = length
    super(Sequence, self).__init__(**kwargs)

  @property
  def feature(self):
    """The inner feature."""
    return self._feature

  def get_tensor_info(self):
    """See base class for details."""
    # Add the additional length dimension to every shape
    def add_length_dim(tensor_info):
      tensor_info = feature_lib.TensorInfo.copy_from(tensor_info)
      tensor_info.shape = (self._length,) + tensor_info.shape
      return tensor_info

    tensor_info = self._feature.get_tensor_info()
    return utils.map_nested(add_length_dim, tensor_info)

  def get_serialized_info(self):
    """See base class for details."""
    # Add the additional length dimension to every serialized features

    def add_length_dim(tensor_info):
      """Add the length dimension to the serialized_info."""
      return feature_lib.TensorInfo(
          shape=(self._length,) + tensor_info.shape,
          dtype=tensor_info.dtype,
      )

    tensor_info = self._feature.get_serialized_info()
    return utils.map_nested(add_length_dim, tensor_info)

  def encode_example(self, example_dict):
    # Convert nested dict[list] into list[nested dict]
    sequence_elements = _transpose_dict_list(example_dict)

    # If length is static, ensure that the given length match
    if self._length is not None and len(sequence_elements) != self._length:
      raise ValueError(
          'Input sequence length do not match the defined one. Got {} != '
          '{}'.format(len(sequence_elements), self._length)
      )

    # Empty sequences return empty arrays
    if not sequence_elements:
      def _build_empty_np(serialized_info):
        return np.empty(
            shape=tuple(s if s else 0 for s in serialized_info.shape),
            dtype=serialized_info.dtype.as_numpy_dtype,
        )

      return utils.map_nested(_build_empty_np, self.get_serialized_info())

    # Encode each individual elements
    sequence_elements = [
        self.feature.encode_example(sequence_elem)
        for sequence_elem in sequence_elements
    ]

    # Then merge the elements back together
    def _stack_nested(sequence_elements):
      if isinstance(sequence_elements[0], dict):
        return {
            # Stack along the first dimension
            k: _stack_nested(sub_sequence)
            for k, sub_sequence in utils.zip_dict(*sequence_elements)
        }
      return stack_arrays(*sequence_elements)

    return _stack_nested(sequence_elements)

  def decode_example(self, tfexample_dict):
    # Note: This all works fine in Eager mode (without tf.function) because
    # tf.data pipelines are always executed in Graph mode.

    # Apply the decoding to each of the individual feature.
    return tf.map_fn(
        self.feature.decode_example,
        tfexample_dict,
        dtype=self.dtype,
        parallel_iterations=10,
        back_prop=False,
        name='sequence_decode',
    )

  def save_metadata(self, *args, **kwargs):
    """See base class for details."""
    self._feature.save_metadata(*args, **kwargs)

  def load_metadata(self, *args, **kwargs):
    """See base class for details."""
    self._feature.load_metadata(*args, **kwargs)

  def __getitem__(self, key):
    """Convenience method to access the underlying features."""
    return self._feature[key]

  def __getattr__(self, key):
    """Allow to access the underlying attributes directly."""
    return getattr(self._feature, key)

  # The __getattr__ method triggers an infinite recursion loop when loading a
  # pickled instance. So we override that name in the instance dict, and remove
  # it when unplickling.
  def __getstate__(self):
    state = self.__dict__.copy()
    state['__getattr__'] = 0
    return state

  def __setstate__(self, state):
    del state['__getattr__']
    self.__dict__.update(state)

  def _additional_repr_info(self):
    """Override to return addtional info to go into __repr__."""
    return {'feature': repr(self._feature)}


def stack_arrays(*elems):
  if isinstance(elems[0], np.ndarray):
    return np.stack(elems)
  else:
    return [e for e in elems]


def np_to_list(elem):
  """Returns list from list, tuple or ndarray."""
  if isinstance(elem, list):
    return elem
  elif isinstance(elem, tuple):
    return list(elem)
  elif isinstance(elem, np.ndarray):
    return list(elem)
  else:
    raise ValueError(
        'Input elements of a sequence should be either a numpy array, a '
        'python list or tuple. Got {}'.format(type(elem)))


def _transpose_dict_list(dict_list):
  """Transpose a nested dict[list] into a list[nested dict]."""
  # 1. Unstack numpy arrays into list
  dict_list = utils.map_nested(np_to_list, dict_list, dict_only=True)

  # 2. Extract the sequence length (and ensure the length is constant for all
  # elements)
  length = {'value': None}  # dict because `nonlocal` is Python3 only
  def update_length(elem):
    if length['value'] is None:
      length['value'] = len(elem)
    elif length['value'] != len(elem):
      raise ValueError(
          'The length of all elements of one sequence should be the same. '
          'Got {} != {}'.format(length['value'], len(elem)))
    return elem
  utils.map_nested(update_length, dict_list, dict_only=True)

  # 3. Extract each individual elements
  return [
      utils.map_nested(lambda elem: elem[i], dict_list, dict_only=True)   # pylint: disable=cell-var-from-loop
      for i in range(length['value'])
  ]
