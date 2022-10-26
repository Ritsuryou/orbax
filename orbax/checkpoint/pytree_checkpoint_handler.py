# Copyright 2022 The Orbax Authors.
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

"""PyTreeCheckpointHandler class.

Implementation of CheckpointHandler interface.
"""

import asyncio
import functools
import os
import re
from typing import Any, List, Optional, Tuple

from absl import logging
from etils import epath
import flax
from flax import traverse_util
import jax
from jax.experimental import multihost_utils

import jax.numpy as jnp
import numpy as np
from orbax.checkpoint import aggregate_handlers
from orbax.checkpoint import lazy_array
from orbax.checkpoint import transform_utils
from orbax.checkpoint import type_handlers
from orbax.checkpoint import utils
from orbax.checkpoint.async_checkpoint_handler import AsyncCheckpointHandler
from orbax.checkpoint.future import Future
import tensorstore as ts


PyTree = type(jax.tree_util.tree_structure(None))
RestoreArgs = type_handlers.RestoreArgs
ArrayRestoreArgs = type_handlers.ArrayRestoreArgs
SaveArgs = type_handlers.SaveArgs
ParamInfo = type_handlers.ParamInfo
TypeHandler = type_handlers.TypeHandler
AggregateHandler = aggregate_handlers.AggregateHandler
MsgpackHandler = aggregate_handlers.MsgpackHandler

_TYPE_METADATA_FILE = 'type_metadata'
_CHECKPOINT_FILE = 'checkpoint'

utils.register_ts_spec_for_serialization()


def _validate_save_args(args: SaveArgs, enable_aggregation: bool):
  if args.aggregate and not enable_aggregation:
    msg = ('Saving to aggregate checkpoint is disabled for this handler, but '
           'aggregation was requested for at least one parameter.')
    raise ValueError(msg)


async def _create_param_save_dir(param_info: ParamInfo, args: SaveArgs):
  tspec = param_info.tspec
  # Directory will be unused.
  if tspec is None or args.aggregate:
    return
  path = tspec['kvstore']['path']
  if jax.process_index() == 0:
    await utils.async_makedirs(epath.Path(path))


def _array_cast(arr, dtype):
  if dtype is not None:
    if utils.is_scalar(arr):
      arr = np.asarray(arr).astype(dtype).item()
    else:
      arr = arr.astype(dtype)
  return arr


def _get_param_names(item: PyTree) -> PyTree:
  """Gets parameter names for PyTree elements."""
  state_dict = utils.to_state_dict(item)
  names = traverse_util.unflatten_dict({
      k: '.'.join(k)
      for k in traverse_util.flatten_dict(state_dict, keep_empty_nodes=True)
  })
  return flax.serialization.from_state_dict(item, names)


def _get_param_infos_from_structure(directory: epath.Path,
                                    structure: PyTree) -> PyTree:
  """Construct ParamInfos based on a PyTree."""
  names = _get_param_names(structure)

  def _get_param_info(leaf, name):
    if isinstance(leaf, utils.Leaf):
      # Leaf is a param name.
      path = os.fspath(directory / leaf)
      tspec = serialization.get_tensorstore_spec(path)
    # The following is kept for backwards compatibility.
    elif isinstance(leaf, ts.Spec):
      tspec = leaf.to_json()  # pytype: disable=attribute-error
      # Skip '.', since we need special regex handling for this char.
      pattern = r'\.' + utils.TMP_DIR_SUFFIX[1:] + r'\d+'
      tspec['kvstore']['path'] = re.sub(pattern, '', tspec['kvstore']['path'])
    elif isinstance(leaf, (int, float, np.number, np.ndarray, jnp.ndarray)):
      # Array already restored, do not need ts.Spec.
      tspec = None
    else:
      raise ValueError(f'Unsupported type: {type(leaf)}')
    return ParamInfo(name=name, aggregate=(tspec is None), tspec=tspec)

  return jax.tree_util.tree_map(_get_param_info, structure, names)


def _get_tree_for_aggregation(param_infos, save_args, item):
  """Get tree for aggregated checkpoint."""

  def _get_leaf_for_aggregation(param_info, arg, arr):
    if arg is None:
      arg = SaveArgs()
    if arg.aggregate:  # Param was aggregated, return value after cast.
      return _array_cast(arr, arg.dtype)
    else:
      return param_info.name

  return jax.tree_util.tree_map(_get_leaf_for_aggregation, param_infos,
                                save_args, item)


def _transform_structure(
    item: PyTree, restored: PyTree, param_infos: PyTree, transforms: PyTree,
    transforms_default_to_original: bool) -> Tuple[PyTree, PyTree]:
  """Transforms `restored` and `param_infos` into the structure of `item`.

  After restoring a checkpoint structure (represented by `restored`), we must
  transform it to match the structure of `item` and fill in any missing values.

  Note that `param_infos` must also be transformed since parameter names and
  other information is computed based on the PyTree structure. We first create
  `param_infos` based on the checkpoint structure, otherwise we would not find
  some parameters after doing transformations and rearranging the tree
  structure.

  Args:
    item: a PyTree representing the result structure ("new tree structure").
    restored: a PyTree representing the original tree structure.
    param_infos: A PyTree of ParamInfo having the same structure as `restored`.
    transforms: provides instructions on how to transform the input trees. See
      transform_utils.
    transforms_default_to_original: See transform_utils.

  Returns:
    A pair of `item`, `param_infos` which have been transformed from the
    original trees using `transforms`.
  """
  if item is None:
    if transforms is not None:
      msg = ('If providing `transforms`, must provide `item` matching structure'
             ' of expected result.')
      raise ValueError(msg)
    item = restored
  else:
    if transforms is None:
      item = flax.serialization.from_state_dict(item, restored)
      param_infos = flax.serialization.from_state_dict(item, param_infos)
    else:
      if transform_utils.has_value_functions(transforms):
        raise ValueError(
            'Found disallowed `value_fn` or `multi_value_fn` in `transforms`.')
      item = transform_utils.apply_transformations(
          restored, transforms, item, transforms_default_to_original)
      # param_infos must be transformed because the ts.Spec of saved params
      # may correspond to the original structure rather than the new.
      param_infos = transform_utils.apply_transformations(
          param_infos, transforms, item, transforms_default_to_original)

      def _create_param_info_if_already_restored(x):
        return ParamInfo(aggregate=True) if not isinstance(x, ParamInfo) else x

      param_infos = jax.tree_util.tree_map(
          _create_param_info_if_already_restored, param_infos)
  return item, param_infos


class PyTreeCheckpointHandler(AsyncCheckpointHandler):
  """A CheckpointHandler implementation for any PyTree structure.

  The PyTree is assumed to be a nested dictionary with array values represented
  as `GlobalDeviceArray` objects or `np.ndarray` or `jnp.ndarray`. If not
  `GlobalDeviceArray`, arrays are expected to be non-partitioned.
  """

  def __init__(self,
               # TODO(b/255434127) Move to Pax code.
               enable_aggregation: bool = True,
               aggregate_filename: Optional[str] = None):
    """Creates PyTreeCheckpointHandler.

    Args:
      enable_aggregation: if False, an aggregated checkpoint file storing the
        PyTree structure will not be created, and the structure must be inferred
        from the directory layout.
      aggregate_filename: name that the aggregated checkpoint should be saved
        as.
    """
    if jax.config.jax_parallel_functions_output_gda and jax.config.jax_array:
      logging.warning(
          '`jax_parallel_functions_output_gda` and `jax_array` '
          'flags are both `True`, so flipping the '
          '`jax_parallel_functions_output_gda` flag to False. To remove this '
          'warning, please set `jax_parallel_functions_output_gda` flag to '
          'False in your project.')
      jax.config.update('jax_parallel_functions_output_gda', False)
    self._aggregate_handler = aggregate_handlers.get_aggregate_handler()
    self._enable_aggregation = enable_aggregation
    if aggregate_filename is None:
      aggregate_filename = _CHECKPOINT_FILE
    self._aggregate_filename = aggregate_filename

  def _get_param_names(self, item: PyTree) -> PyTree:
    """Gets parameter names for PyTree elements."""
    return _get_param_names(item)

  def _get_param_infos(self, item: PyTree, directory: epath.Path,
                       save_args: PyTree) -> PyTree:
    """Returns parameter information for elements in `item`.

    At minimum, this method should extract the names of each parameter for
    saving/restoring.

    Args:
      item: a PyTree to extract information from.
      directory: a directory where checkpoint files are located.
      save_args: PyTree matching item containing SaveArgs.

    Returns:
      A PyTree matching `item` of ParamInfo.
    """
    if not item:
      raise ValueError('Found empty item')
    names = self._get_param_names(item)

    def _param_info(name, value, args):
      if args.aggregate:
        return ParamInfo(name=name, aggregate=True)
      return type_handlers.get_type_handler(type(value)).param_info(
          directory, name, value)

    return jax.tree_util.tree_map(_param_info, names, item, save_args)

  async def async_save(
      self,
      directory: epath.Path,
      item: PyTree,
      save_args: Optional[PyTree] = None) -> Optional[List[Future]]:
    """Saves a PyTree from a given training step.

    This operation is compatible with a multi-host, multi-device setting. Array
    values must be represented as `GlobalDeviceArray` or `np.ndarray`.

    After saving, all files will be located in directory/

    Saves an additional file to directory/checkpoint on host 0 which
    contains the serialized structure of `item`, along with any parameters that
    request Flax serialization.

    Args:
      directory: save location directory.
      item: a PyTree to be saved.
      save_args: a PyTree matching `item` which consists of SaveArgs objects as
        values.

    Returns:
      A Future that will commit the data to `directory` when awaited. Copying
      the data from its source will be awaited in this function.
    """
    item = await lazy_array.maybe_get_tree_async(item)

    if save_args is None:
      save_args = jax.tree_util.tree_map(lambda x: SaveArgs(), item)
    jax.tree_util.tree_map(
        functools.partial(
            _validate_save_args, enable_aggregation=self._enable_aggregation),
        save_args)

    param_infos = self._get_param_infos(item, directory, save_args)
    # Create directories in parallel.
    await asyncio.gather(*jax.tree_util.tree_flatten(
        jax.tree_util.tree_map(_create_param_save_dir, param_infos, save_args))
                         [0])
    multihost_utils.sync_global_devices(
        'PyTreeCheckpointHandler:create_param_save_dirs')

    async def serialize(value, info, args):
      if args.aggregate:
        return  # skip serialize now, include in aggregated file
      handler = type_handlers.get_type_handler(type(value))
      return await handler.serialize(value, info, args)

    future_tree = jax.tree_util.tree_map(serialize, item, param_infos,
                                         save_args)
    copy_futures, _ = jax.tree_util.tree_flatten(future_tree)
    assert isinstance(copy_futures, list)
    # Await copy futures.
    commit_futures = await asyncio.gather(*copy_futures)
    commit_futures, _ = jax.tree_util.tree_flatten(commit_futures)

    ser_item = _get_tree_for_aggregation(param_infos, save_args, item)
    await self._aggregate_handler.serialize(
        directory / self._aggregate_filename, ser_item)

    return commit_futures

  def save(self, directory: epath.Path, item: Any, *args, **kwargs):
    """Saves the provided item.

    Blocks until both copy and commit complete.

    See async_save.

    Args:
      directory: the directory to save to.
      item: the item to be saved.
      *args: additional arguments for save.
      **kwargs: additional arguments for save.
    """

    async def async_save(*args, **kwargs):
      commit_futures = await self.async_save(*args, **kwargs)
      # Futures are already running, so sequential waiting is equivalent to
      # concurrent waiting.
      for future in commit_futures:
        future.result()  # Block on result.

    asyncio.run(async_save(directory, item, *args, **kwargs))
    multihost_utils.sync_global_devices('PyTreeCheckpointHandler:save')

  async def _maybe_deserialize(self, info: ParamInfo, value: Any,
                               args: RestoreArgs) -> Any:
    """Deserializes using handler or returns already restored value.

    If the ParamInfo indicates that the parameter was aggregated, then it must
    have already been restored. In this case, we simply perform a cast and
    convert to LazyArray if requested.

    Otherwise, we deserialize using an appropriate TypeHandler, converting to
    LazyArray and casting if requested.

    Args:
      info: ParamInfo
      value: a tree value which may have already been restored. Not relevant if
        info.aggregate is False.
      args: RestoreArgs for TypeHandler restoration.

    Returns:
      A deserialized parameter.
    """
    if info.aggregate:  # Already restored from AggregateHandler.
      value = _array_cast(value, args.dtype)
      if args.lazy:
        value = lazy_array.LazyAwaitableArray.from_array(
            value, dtype=args.dtype)
      return value
    handler = type_handlers.get_type_handler(args.restore_type)
    arr = lazy_array.LazyAwaitableArray.from_tensor_store_spec(
        ts.Spec(info.tspec),
        get_fn=lambda: handler.deserialize(info, args),
        dtype=args.dtype)
    if not args.lazy:
      arr = await arr.get_async()
    return arr

  def restore(self,
              directory: epath.Path,
              item: Optional[PyTree] = None,
              restore_args: Optional[PyTree] = None,
              transforms: Optional[PyTree] = None,
              transforms_default_to_original: bool = True) -> PyTree:
    """Restores a PyTree from the checkpoint directory at the given step.

    Optional arguments meshes and mesh_axes define how each array in the
    restored tree should be partitioned. For more information, see below and see
    pjit documentation.

    Args:
      directory: save location directory.
      item: provides the structure for the restored item. If not provided, will
        infer the structure from the saved checkpoint. Transformations will not
        be run.
      restore_args: optional object containing additional arguments for
        restoration. It should be a PyTree matching the structure of `item`, and
        should contain a RestoreArgs object for every value. If `item` is not
        provided, should match the structure of the checkpoint.
      transforms: a PyTree of transformations that should be applied to the
        saved item in order to obtain a final structure. See `transform_utils`
        for further information.
      transforms_default_to_original: See transform_utils.apply_transformations.

    Returns:
      A PyTree matching the structure of `item` with restored array values as
      `GlobalDeviceArray` or `jax.Array` if `as_jax_array` or `np.ndarray`
      otherwise. If `lazy` restoration is enabled, `LazyArray` will be returned.

    Raises:
      FileNotFoundError: `directory` does not exist or is missing required files
      ValueError: `transforms` is provided without `item`.
      ValueError: `transforms` contains elements with `value_fn` or
        `multi_value_fn`.
    """
    if not directory.exists():
      raise FileNotFoundError(
          f'Requested directory for restore does not exist at {directory}')

    restored = self.structure(directory)
    param_infos = _get_param_infos_from_structure(directory, restored)
    item, param_infos = _transform_structure(item, restored, param_infos,
                                             transforms,
                                             transforms_default_to_original)

    if restore_args is None:
      restore_args = jax.tree_util.tree_map(lambda x: RestoreArgs(), item)

    future_arrays = jax.tree_map(self._maybe_deserialize, param_infos, item,
                                 restore_args)
    future_arrays, item_def = jax.tree_util.tree_flatten(future_arrays)

    async def _async_restore(future_arrays):
      return await asyncio.gather(*future_arrays)

    result = asyncio.run(_async_restore(future_arrays))
    restored_item = jax.tree_util.tree_unflatten(item_def, result)

    multihost_utils.sync_global_devices('PyTreeCheckpointHandler:restore')
    return restored_item

  def structure(self, directory: epath.Path) -> PyTree:
    """Restores the saved PyTree structure without regard for its leaf values.

    Args:
      directory: the directory to restore from.

    Returns:
      The structure of the checkpointed PyTree. Leaves may be of any type.

    Raises:
      FileNotFoundError: if the checkpoint is not found.
    """
    if (directory / self._aggregate_filename).exists():
      return self._aggregate_handler.deserialize(directory /
                                                 self._aggregate_filename)
    else:
      if self._enable_aggregation:
        raise FileNotFoundError(f'Checkpoint does not exist at {directory}.')
      else:
        return utils.pytree_structure(directory)
